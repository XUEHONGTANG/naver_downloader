"""
Naver Premium Contents 文章下載器（圖形介面版）

給一般使用者：雙擊執行 → 按「開始」→ 在跳出的瀏覽器登入自己的 Naver 帳號
→ 程式自動把文章存成 .md 檔。
每個人只能下載自己帳號有權限看到的文章，僅供個人備份。
"""
import html
import queue
import re
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.parse import parse_qs, urlparse

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


LIST_API = BASE + "/ch/template/SCS_PREMIUM_CONTENT_LIST"
ITEM_RE = re.compile(r'<li class="content_item.*?</li>', re.S)


def _text(pattern, s):
    m = re.search(pattern, s, re.S)
    return html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip() if m else ""


def list_via_api(page, list_url, log):
    """直接呼叫網站載入列表用的 API，一頁一頁讀到最後；回傳 {網址: (標題, 分類)}
    網址帶 categoryId 時只保留該分類（API 本身不會篩選，所以自己篩）"""
    cp, sub = channel_prefix(list_url).strip("/").split("/")
    want_cat = parse_qs(urlparse(list_url).query).get("categoryId", [""])[0]
    seen, count, cursor = {}, 0, ""
    while True:
        r = page.request.get(LIST_API, params={"cpName": cp, "subId": sub, "lastContentId": cursor})
        h = r.json()["renderedComponent"]["SCS_PREMIUM_CONTENT_LIST"]
        before = count
        for item in ITEM_RE.findall(h):
            m = re.search(rf'href="(/{cp}/{sub}/contents/[0-9A-Za-z]+)"', item)
            if not m:
                continue
            count += 1
            cat_id = re.search(r'categoryId&#x3D;([0-9A-Za-z]+)" class="content_category"', item)
            if want_cat and (not cat_id or cat_id.group(1) != want_cat):
                continue
            seen.setdefault(BASE + m.group(1), (
                _text(r'<strong class="content_title">(.*?)</strong>', item),
                _text(r'class="content_category"[^>]*>(.*?)</a>', item)))
        log(f"已讀取 {count} 篇，符合 {len(seen)} 篇…")
        m = re.search(r'data-cursor="([^"]*)" data-has-next="(\w+)"', h)
        if not m or m.group(2) != "true" or not m.group(1) or count == before:
            return seen
        cursor = m.group(1)
        time.sleep(0.5)


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
                    continue
                log(f"讀取列表：{u}")
                is_root = bool(CHANNEL_ROOT_RE.match(u))
                try:
                    # 頻道首頁：依每篇文章的分類放資料夾；列表網址：全部放同一個資料夾
                    for k, (title, cat) in list_via_api(page, u, log).items():
                        links.setdefault(k, (title, cat if is_root else ""))
                except Exception as e:
                    log(f"API 讀取失敗（{e}），改用捲動網頁的方式…")
                    if is_root:
                        for cat, cat_url in find_categories(page, u, log):
                            log(f"讀取分類：{cat}")
                            for k, v in collect_links(page, cat_url, log).items():
                                links.setdefault(k, (v, cat))
                    else:
                        for k, v in collect_links(page, u, log).items():
                            links.setdefault(k, (v, ""))
            if not links:
                raise RuntimeError("沒有找到任何文章，網址可能有誤或網站改版。")
            log(f"共找到 {len(links)} 篇文章，開始下載…")

            # 用檔案裡的「來源」網址判斷是否下載過，標題相同的不同文章也不會互相覆蓋
            existing = {}
            for f in out.rglob("*.md"):
                m = re.search(r"^來源：(\S+)", f.read_text(encoding="utf-8", errors="ignore"), re.M)
                if m:
                    existing[m.group(1)] = f
            for i, (url, (title, cat)) in enumerate(links.items(), 1):
                if url in existing:
                    log(f"[{i}/{len(links)}] 已存在，略過：{title or url}")
                    continue
                t, text = fetch_article(page, url)
                name = safe_name(t or title)
                folder = out / safe_name(cat) if cat else out
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / f"{name}.md"
                if path.exists():
                    path = folder / f"{name}_{url.rsplit('/', 1)[-1]}.md"
                existing[url] = path
                path.write_text(
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
