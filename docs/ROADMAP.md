# نقشه راه NTTM_PLUS

## فاز ۰ — جداسازی موتور از انتقال (انجام شد)
- [x] اینترفیس `FetchEngine` + قرارداد `FetchResult` در `client/engine.py`
- [x] کارخانه `create_engine()` و رجیستری موتورها (قابل توسعه از UI)
- [x] اتصال `main.py` به کارخانه با آرگومان `--engine {github,local}`
- [x] انتشار با PR #1 (merge شده به main)
- [ ] انتقال تعریف `FetchResult` از `transport.py` به `engine.py` (بدون تغییر رفتار) — موکول به یک refactor جدا

## فاز ۱ — موتور محلی (انجام شد)
- [x] `LocalEngine` با Playwright محلی: ناوبری، رندر، ذخیره snapshot صفحه + capture منابع (css/js/img/font) و بازنویسی مسیرها برای رندر آفلاین
- [x] انتقال سشن (کوکی‌ها / localStorage / sessionStorage) با `SessionManager`
- [x] اکشن‌های SPA، تأخیر دلخواه، اسکرول خودکار، حالت form و ajax_fallback
- [x] دانلود فایل (رویداد download + تشخیص content-disposition) با همان قرارداد `FetchResult`
- [x] حالت CDP: اتصال به مرورگر بازِ کاربر (`config.local_cdp_url` یا متغیر محیطی `NTTM_CDP_URL`)
- [x] فیلدهای جدید config: `local_headless` و `local_cdp_url`

### نحوه استفاده
```bash
pip install playwright
python -m playwright install chromium
python main.py --engine local

# یا اتصال به مرورگر باز خودتان:
chrome --remote-debugging-port=9222
NTTM_CDP_URL=http://127.0.0.1:9222 python main.py --engine local
```

## فاز ۲ — یکپارچه‌سازی موتور با UI موجود (انجام شد)
> برنامه قبلی (پنل CustomTkinter) منسوخ شد: UI اصلی NTTM از قبل یک مرورگر گرافیکی کامل PyWebView است و به‌دلیل معماری فاز ۰ با موتور local هم کار می‌کند.
- [x] انتخابگر موتور (GitHub / Local) داخل مودال Settings بدون نیاز به فلگ CLI
- [x] فیلدهای تنظیمات موتور محلی (headless و CDP URL) در همان مودال
- [x] نشانگر موتور فعال (Engine Badge) در نوار وضعیت پایین + پیام‌های status موتور-آگاه
- [x] سوییچ موتور در لحظه (بدون ری‌استارت برنامه) از طریق `save_local_settings`
- [x] ذخیره موتور انتخابی در `config.json` (فیلد `engine`) — فلگ `--engine` همچنان override می‌کند
- [x] گارد امن متدهای Daemon برای موتورهایی که رانر دائمی ندارند

## فاز ۳ — گیت‌هاب به‌عنوان fallback
- [ ] `GitHubTransport` فقط یکی از گزینه‌ها می‌ماند (کد موجود پشت اینترفیس فاز ۰)

## بعداً (P3)
پروکسی HTTP محلی، افزونه‌ی مرورگر، transportهای دیگر، مدیریت چند اکانت.
