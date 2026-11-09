"""
Naver Premium Contents 文章下載器（圖形介面版）

給一般使用者：雙擊執行 → 按「開始」→ 在跳出的瀏覽器登入自己的 Naver 帳號
→ 程式自動把文章存成 .md 檔。
每個人只能下載自己帳號有權限看到的文章，僅供個人備份。
"""
import queue
import re
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

DEFAULT_URL = "https://contents.premium.naver.com/willam/william"
PROFILE_DIR = Path.home() / ".naver_downloader_profile"
BASE = "https://contents.premium.naver.com"


# ---------------- 抓取邏輯 ----------------
def channel_prefix(list_url):
    """從列表網址取出頻道路徑，例如 /willam/william"""
    parts = [p for p in urlparse(list_url).path.split("/") if p]
    if len(parts) < 2:
        raise ValueError("網址格式不對，應類似 https://contents.premium.naver.com/xxx/yyy/contents")
    return f"/{parts[0]}/{parts[1]}"


def launch(p):
    """優先用電腦上已安裝的 Edge / Chrome，免下載瀏覽器"""
    for ch in ("msedge", "chrome", None):
        try:
            kw = {"channel": ch} if ch else {}
            return p.chromium.launch_persistent_context(
                str(PROFILE_DIR), headless=False, **kw
            )
        except Exception:
            continue
    raise RuntimeError("找不到 Edge 或 Chrome，請先安裝其中一個瀏覽器。")


def logged_in(ctx):
    return any(c["name"] == "NID_AUT" for c in ctx.cookies("https://naver.com"))


def collect_links(page, list_url, log, max_scrolls=80):
    pattern = re.compile(re.escape(channel_prefix(list_url)) + r"/contents/[0-9A-Za-z]+")
    page.goto(list_url, wait_until="networkidle")
    seen, stable = {}, 0
    for _ in range(max_scrolls):
        for a in page.query_selector_all("a[href]"):
            m = pattern.search(a.get_attribute("href") or "")
            if m:
                url = BASE + m.group(0)
                title = (a.inner_text() or "").strip().split("\n")[0]
                if url not in seen or (title and not seen[url]):
                    seen[url] = title
        before = len(seen)
        page.mouse.wheel(0, 4000)
        time.sleep(1.5)
        more = page.query_selector("button:has-text('더보기')")
        if more:
            try:
                more.click()
                time.sleep(1.5)
            except Exception:
                pass
        stable = stable + 1 if len(seen) == before else 0
        log(f"目前找到 {len(seen)} 篇…")
        if stable >= 3:
            break
    return seen


def fetch_article(page, url):
    page.goto(url, wait_until="networkidle")
    title = page.title().replace(" : 네이버 프리미엄콘텐츠", "").strip()
    for sel in (".se-main-container", "article", "main", "body"):
        el = page.query_selector(sel)
        if el and len(el.inner_text().strip()) > 200:
            return title, el.inner_text().strip()
    return title, page.inner_text("body")


def safe_name(s):
    return re.sub(r'[\\/:*?"<>|\r\n]', "_", s).strip()[:80] or "untitled"


CHANNEL_ROOT_RE = re.compile(r"^https?://contents\.premium\.naver\.com/[^/?#]+/[^/?#]+/?(?:[?#].*)?$")


def find_categories(page, root_url, log):
    """從「所有分類」頁 /頻道/category 找出各分類的網址；回傳 [(分類名, 網址), ...]"""
    prefix = channel_prefix(root_url)
    cat_page = BASE + prefix + "/category"
    page.goto(cat_page, wait_until="networkidle")
    # 分類頁可能也是往下捲才載入完
    for _ in range(5):
        page.mouse.wheel(0, 4000)
        time.sleep(1)
    cats = {}
    for a in page.query_selector_all("a[href]"):
        href = (a.get_attribute("href") or "").split("#")[0]
        if href.startswith("/"):
            href = BASE + href
        if not href.startswith(BASE + prefix) or ARTICLE_URL_RE.match(href):
            continue
        rest = href[len(BASE + prefix):]
        # 分類連結可能是 /category/xxx、/category?xxx 或 /contents?categoryId=xxx
        is_cat = (rest.startswith("/category") and rest.rstrip("/") != "/category") or \
                 (rest.startswith("/contents?") and "categor" in rest.lower())
        if is_cat:
            name = (a.inner_text() or "").strip().split("\n")[0]
            if name and href not in cats.values():
                cats[name] = href
    log(f"找到 {len(cats)} 個分類：{'、'.join(cats) or '（無）'}")
    # 最後再掃一次「全部文章」，補上沒有分類的
    return list(cats.items()) + [("未分類", BASE + prefix + "/contents")]


ARTICLE_URL_RE = re.compile(r"^https?://contents\.premium\.naver\.com/[^/]+/[^/]+/contents/[0-9A-Za-z]+")


def run_job(urls, out_dir, login_event, log, done, skip_login=False):
    try:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            ctx = launch(p)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            if not skip_login and not logged_in(ctx):
                page.goto("https://nid.naver.com/nidlogin.login")
                log("請在跳出的瀏覽器登入 Naver，登入後按「我已登入」。")
                login_event.wait()
            if logged_in(ctx):
                log("已登入，開始讀取文章列表…")
            else:
                log("未登入，只能下載公開內容，付費文章可能只有預覽。開始讀取文章列表…")

            # 每行可以是：單篇文章網址 / 頻道首頁（依分類全抓）/ 某個列表網址
            links = {}  # url -> (title, 分類資料夾)
            for u in urls:
                m = ARTICLE_URL_RE.match(u)
                if m:
                    links.setdefault(m.group(0), ("", ""))
                elif CHANNEL_ROOT_RE.match(u):
                    for cat, cat_url in find_categories(page, u, log):
                        log(f"讀取分類：{cat}")
                        for k, v in collect_links(page, cat_url, log).items():
                            links.setdefault(k, (v, cat))
                else:
                    log(f"讀取列表：{u}")
                    for k, v in collect_links(page, u, log).items():
                        links.setdefault(k, (v, ""))
            if not links:
                raise RuntimeError("沒有找到任何文章，網址可能有誤或網站改版。")

            existing = {f.stem for f in out.rglob("*.md")}
            for i, (url, (title, cat)) in enumerate(links.items(), 1):
                if title and safe_name(title) in existing:
                    log(f"[{i}/{len(links)}] 已存在，略過：{title}")
                    continue
                t, text = fetch_article(page, url)
                name = safe_name(t or title)
                folder = out / safe_name(cat) if cat else out
                folder.mkdir(parents=True, exist_ok=True)
                (folder / f"{name}.md").write_text(
                    f"# {t}\n\n來源：{url}\n\n{text}\n", encoding="utf-8"
                )
                log(f"[{i}/{len(links)}] {t}")
                time.sleep(2)
            ctx.close()
        done(f"完成！共 {len(links)} 篇，存在：\n{out}")
    except Exception as e:
        done(f"發生錯誤：{e}", error=True)


# ---------------- 介面 ----------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Naver 付費文章下載器")
        self.geometry("560x420")
        self.q = queue.Queue()
        self.login_event = threading.Event()

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="網址（一行一個；可貼單篇文章網址，或頻道列表網址下載全部）").grid(
            row=0, column=0, columnspan=2, sticky="w")
        self.url = tk.Text(frm, height=4, width=60)
        self.url.insert("1.0", DEFAULT_URL)
        self.url.grid(row=1, column=0, columnspan=2, sticky="we")

        ttk.Label(frm, text="存放資料夾").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.out = tk.StringVar(value=str(Path.home() / "Desktop" / "Naver文章"))
        ttk.Entry(frm, textvariable=self.out, width=48).grid(row=3, column=0, sticky="we")
        ttk.Button(frm, text="選擇…", command=self.pick).grid(row=3, column=1)

        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, columnspan=2, pady=10, sticky="w")
        self.start_btn = ttk.Button(btns, text="開始", command=self.start)
        self.start_btn.pack(side="left")
        self.login_btn = ttk.Button(btns, text="我已登入", command=self.login_event.set, state="disabled")
        self.login_btn.pack(side="left", padx=8)
        self.skip_login = tk.BooleanVar(value=False)
        ttk.Checkbutton(btns, text="不登入（只下載公開內容）", variable=self.skip_login).pack(side="left")

        self.logbox = tk.Text(frm, height=14, state="disabled")
        self.logbox.grid(row=5, column=0, columnspan=2, sticky="nsew")
        frm.columnconfigure(0, weight=1)
        frm.rowconfigure(5, weight=1)

        self.after(200, self.poll)

    def pick(self):
        d = filedialog.askdirectory()
        if d:
            self.out.set(d)

    def log(self, msg):
        self.q.put(("log", msg))

    def done(self, msg, error=False):
        self.q.put(("done", (msg, error)))

    def start(self):
        urls = [l.strip() for l in self.url.get("1.0", "end").splitlines() if l.strip()]
        if not urls:
            messagebox.showwarning("下載器", "請至少輸入一個網址")
            return
        self.start_btn.config(state="disabled")
        skip = self.skip_login.get()
        self.login_btn.config(state="disabled" if skip else "normal")
        self.login_event.clear()
        threading.Thread(
            target=run_job,
            args=(urls, self.out.get().strip(), self.login_event, self.log, self.done, skip),
            daemon=True,
        ).start()

    def poll(self):
        while not self.q.empty():
            kind, data = self.q.get()
            if kind == "log":
                self.logbox.config(state="normal")
                self.logbox.insert("end", data + "\n")
                self.logbox.see("end")
                self.logbox.config(state="disabled")
            else:
                msg, error = data
                self.start_btn.config(state="normal")
                self.login_btn.config(state="disabled")
                (messagebox.showerror if error else messagebox.showinfo)("下載器", msg)
        self.after(200, self.poll)


if __name__ == "__main__":
    App().mainloop()
