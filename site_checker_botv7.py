"""
Site Checker Bot v10 ULTIMATE
Improvements:
- Phase-based loading bar (real progress)
- Stripe checkout URL detection from redirects + HTML
- Better 2D/3D detection (more signatures)
- Daily scan limit for all users (auto, no UID needed)
- Clean output format
- Chromium auto-detect for Railway
"""

import asyncio, csv, io, json, logging, re, socket, ssl, time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, wait as fut_wait
from contextlib import asynccontextmanager
from datetime import datetime, date
from urllib.parse import urljoin, urlparse
import random, os

import aiosqlite, httpx, requests
from bs4 import BeautifulSoup
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat,
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters,
)
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    from playwright.async_api import async_playwright, Browser
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

try:
    from playwright_stealth import stealth_async
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False

try:
    from curl_cffi import requests as cffi_req
    HAS_CURL_CFFI = True
    CFFI_PROFILES = ["chrome110","chrome116","chrome120","chrome124","firefox117","safari17_0"]
except ImportError:
    HAS_CURL_CFFI = False
    CFFI_PROFILES = []

try:
    import dns.resolver
    HAS_DNS = True
except ImportError:
    HAS_DNS = False

# ══════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════
BOT_TOKEN        = os.environ.get("BOT_TOKEN",  "8748624562:AAGkWrsWAmerFZjNeNdEAKM5ymjnFVBU5RQ")
BOT_AUTHOR       = os.environ.get("BOT_AUTHOR", "SiteChkBot")
ADMIN_IDS        = [int(x) for x in os.environ.get("ADMIN_IDS","1964475260").split(",") if x.strip().isdigit()]
DB_PATH          = "sitechecker.db"
CACHE_TTL        = 6 * 3600          # 6hr cache
DAILY_LIMIT      = 20                 # default scans/day per user (admin can change)
BROWSER_POOL_SZ  = 2
MAX_URLS_MSG     = 20
MAX_URLS_FILE    = 100
REQ_TIMEOUT      = 12
JS_TIMEOUT       = 7
PW_TIMEOUT       = 25_000
WB_TIMEOUT       = 10
GW_THRESHOLD     = 4
PLT_MIN          = 3
MAX_JS           = 40
PROXY_LIST: list[str] = []

# Chromium paths for Railway/Docker
CHROMIUM_PATHS = [
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/ms-playwright/chromium_headless_shell-1208/chrome-linux/headless_shell",
    "/ms-playwright/chromium-1208/chrome-linux/chrome",
    "/ms-playwright/chromium-1169/chrome-linux/chrome",
    "/ms-playwright/chromium-1148/chrome-linux/chrome",
]

def find_chromium() -> str | None:
    for p in CHROMIUM_PATHS:
        if os.path.exists(p):
            return p
    return None

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

_cancel_flags: dict[int, bool] = {}
_active_scans: dict[int, bool] = {}
_monitors:     dict[str, dict] = {}
_last_results: dict[int, list] = defaultdict(list)

def is_scanning(uid):    return _active_scans.get(uid, False)
def set_scanning(uid,v):
    _active_scans[uid] = v
    if not v: _cancel_flags[uid] = False
def request_cancel(uid): _cancel_flags[uid] = True
def is_cancelled(uid):   return _cancel_flags.get(uid, False)

UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
]

FRAMES = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]

# Scan phases — loading bar shows real progress
SCAN_PHASES = [
    (5,  "🌐 Connecting..."),
    (15, "📄 Fetching page..."),
    (30, "🔍 Scanning JS..."),
    (45, "🗂 Extra pages..."),
    (60, "🌐 Browser scan..."),
    (75, "🔐 SSL + Headers..."),
    (88, "🧬 DNS + Webhooks..."),
    (95, "📊 Analyzing..."),
]

# ══════════════════════════════════════════════
#  BROWSER POOL
# ══════════════════════════════════════════════

class BrowserPool:
    def __init__(self, size=BROWSER_POOL_SZ):
        self._size      = size
        self._browsers: list[Browser] = []
        self._sem: asyncio.Semaphore | None = None
        self._pw        = None

    async def start(self):
        if not HAS_PLAYWRIGHT: return
        self._sem = asyncio.Semaphore(self._size)
        chromium  = find_chromium()
        log.info("Chromium: %s", chromium or "auto-detect")
        try:
            self._pw = await async_playwright().__aenter__()
            args = dict(
                headless=True,
                args=["--no-sandbox","--disable-setuid-sandbox",
                      "--disable-blink-features=AutomationControlled",
                      "--disable-dev-shm-usage","--disable-web-security",
                      "--single-process","--no-zygote"],
            )
            if chromium: args["executable_path"] = chromium
            for _ in range(self._size):
                self._browsers.append(await self._pw.chromium.launch(**args))
            log.info("Browser pool: %d instances ✓", self._size)
        except Exception as e:
            log.warning("Browser pool failed: %s", e)

    async def stop(self):
        for b in self._browsers:
            try: await b.close()
            except Exception: pass
        if self._pw:
            try: await self._pw.__aexit__(None, None, None)
            except Exception: pass

    def ok(self): return bool(self._browsers and self._sem)

    @asynccontextmanager
    async def acquire(self):
        if not self._sem or not self._browsers:
            yield None; return
        async with self._sem:
            yield random.choice(self._browsers)

_pool: BrowserPool | None = None

# ══════════════════════════════════════════════
#  GATEWAY SIGNATURES
# ══════════════════════════════════════════════

GW: dict[str, list[tuple[str,int]]] = {
    "Stripe": [
        ("js.stripe.com",8),("stripe.com/v3",8),
        ("Stripe(\"pk_live_",10),("Stripe('pk_live_",10),
        ("Stripe(\"pk_test_",8),("Stripe('pk_test_",8),
        ("pk_live_",6),("pk_test_",4),
        ("stripe.confirmCardPayment",6),("stripe.createPaymentMethod",6),
        ("stripe.confirmPayment",6),("stripe.handleCardAction",6),
        ("stripe.redirectToCheckout",6),("stripe.createToken",6),
        ("stripe.elements(",6),("stripeToken",4),
        ("stripe_publishable_key",6),("STRIPE_PUBLISHABLE_KEY",6),
        ("stripe.com/checkout",6),("checkout.stripe.com",8),
        ("data-stripe",4),("stripe-element",4),
        # Stripe checkout redirect URL
        ("checkout.stripe.com/c/pay/cs_live",10),
        ("checkout.stripe.com/c/pay/cs_test",8),
        ("stripe.com/pay/",6),
    ],
    "PayPal": [
        ("paypal.com/sdk/js",8),("paypal.Buttons(",8),
        ("paypal.com/v2/checkout",8),("paypalobjects.com",6),
        ("PayPalScriptProvider",6),("PAYPAL_CLIENT_ID",6),
        ("paypal_client_id",6),("paypalCheckout",4),
        ("paypal.com/cgi-bin/webscr",6),("paypal.com/checkoutnow",6),
    ],
    "Braintree": [
        ("braintreepayments.com",8),("braintreegateway.com",8),
        ("braintree-web",6),("braintree.client.create",8),
        ("hostedFields.create",6),("braintree.dropin.create",8),
        ("braintree.js",6),("braintree_token",6),
    ],
    "Authorize.Net": [
        ("authorize.net",6),("Accept.js",8),("AcceptUI",8),
        ("AuthorizeNetPopup",8),("anet_params",6),
    ],
    "Adyen": [
        ("checkoutshopper-live.adyen.com",8),
        ("checkoutshopper-test.adyen.com",6),
        ("AdyenCheckout(",8),("adyen.encrypt",6),("adyenConfiguration",6),
        ("adyen.com/hpp",6),
    ],
    "Square": [
        ("payments.squareup.com",8),("squareup.com/v2/square.js",8),
        ("Square.payments(",8),("sq-card-number",6),
        ("sq-payment-form",6),("square_application_id",6),
    ],
    "Checkout.com": [
        ("cdn.checkout.com",8),("Frames.init(",8),
        ("cko-card-number",6),("cko_public_key",6),
    ],
    "Worldpay":   [("cdn.worldpay.com",8),("Worldpay(",8),("worldpay.js",6)],
    "Cybersource":[("cybersource.com",8),("flex-microform",8),("microform.createField",8)],
    "Mollie":     [("app.mollie.com",8),("mollie.createToken",8),("mollieCheckout",6)],
    "Paddle":     [("cdn.paddle.com",8),("Paddle.Setup(",8),("Paddle.Checkout",6)],
    "2Checkout":  [("2checkout.com",8),("TCO.loadCart",8)],
    "BlueSnap":   [("bluesnap.com",8),("hostedPaymentFieldsCreate",8)],
    "NMI":        [("secure.networkmerchants.com",8),("CollectJS",8)],
    "Recurly":    [("js.recurly.com",8),("recurly.configure(",8),("recurly.token",6)],
    "Chargebee":  [("js.chargebee.com",8),("Chargebee.init(",8)],
    "Zuora":      [("static.zuora.com",8),("Z.renderWithErrorHandler",8)],
    "Paysafe":    [("hosted.paysafe.com",8),("paysafe.fields(",8)],
    "Opayo":      [("pi-live.sagepay.com",8),("sagepay.js",6)],
    "Nuvei":      [("nuvei.com",8),("nuvei.js",6)],
    "Heartland":  [("heartlandpaymentsystems.com",8),("Heartland.SecureSubmit",8)],
    "Elavon":     [("elavon.com",8),("convergepay.com",8)],
    "WePay":      [("wepay.com",8),("WePay",6)],
    "Skrill":     [("pay.skrill.com",8),("Skrill",6)],
    "Neteller":   [("neteller.com",8),("NETELLER",6)],
    # BNPL
    "Klarna":   [("klarna.com/eu/payments",8),("KlarnaCheckout(",8),("Klarna.start(",8),("x.klarnacdn.net",8),("klarna.com/us/payments",8)],
    "Afterpay": [("js.afterpay.com",8),("AfterPay.initialize",8),("afterpay.com/v3",6)],
    "Affirm":   [("cdn1.affirm.com",8),("affirm.ui.ready",8),("_affirm_config",8)],
    "Sezzle":   [("widget.sezzle.com",8),("sezzleWidget",8)],
    "Zip":      [("quadpay.com",8),("zip.co/v2",8),("Zip.initialize",8)],
    "Splitit":  [("splitit.com",8),("Splitit.ui",8)],
    "Laybuy":   [("laybuy.com",8),("laybuy.checkout",8)],
    # Wallets
    "Google Pay":  [("pay.google.com/gp/p/js",8),("google-pay-button",6),("GooglePay(",6)],
    "Apple Pay":   [("ApplePaySession",8),("apple-pay-button",6)],
    "Shopify Pay": [("shop.app/pay",8),("ShopPay",8),("shopify_payments",6)],
    "Amazon Pay":  [("payments.amazon.com",8),("AmazonPay",8)],
    "WeChat Pay":  [("pay.weixin.qq.com",8),("wechatpay",6)],
    "Alipay":      [("alipay.com",8),("alipay.trade",8)],
    # India
    "Razorpay":  [("checkout.razorpay.com",8),("Razorpay(",8),("rzp_live_",8),("rzp_test_",6)],
    "Paytm":     [("securegw.paytm.in",8),("PaytmChecksum",8)],
    "Cashfree":  [("sdk.cashfree.com",8),("CashFreeCheckout",8)],
    "Instamojo": [("instamojo.com",8),("Insta.pay",8)],
    "PayU":      [("checkout.payumoney.com",8),("PayU.getEasyPay",8)],
    "CCAvenue":  [("ccavenue.com",8),("ccavReqHandler",8)],
    "Juspay":    [("juspay.in",8),("Juspay",6)],
    # SE Asia
    "Xendit":    [("xendit.co",8),("Xendit.card",8)],
    "GoPay":     [("gopay.com",8),("GoPay",6)],
    "GrabPay":   [("grab.com/sg/pay",8),("GrabPay",8)],
    "Omise":     [("cdn.omise.co",8),("Omise.createToken",8)],
    "2C2P":      [("2c2p.com",8),("2C2P",6)],
    # LatAm
    "Mercado Pago": [("sdk.mercadopago.com",8),("MercadoPago(",8),("mp_public_key",6)],
    "PagSeguro":    [("pagseguro.com.br",8),("PagSeguroDirectPayment",8)],
    "dLocal":       [("dlocalgo.com",8),("dLocal(",8)],
    # Middle East
    "Tap Payments": [("tap.company",8),("Tap(",8),("goSell",6)],
    "HyperPay":     [("hyperpay.com",8),("wpwlOptions",8)],
    "Moyasar":      [("moyasar.com",8),("Moyasar.init",8)],
    "PayTabs":      [("paytabs.com",8),("PayTabs",6)],
    "Geidea":       [("geidea.net",8),("Geidea",6)],
    # Europe
    "iDEAL":     [("ideal.nl",8),("iDEAL",6)],
    "Bancontact": [("bancontact.com",8),("bancontact",4)],
    "Sofort":    [("sofort.com",8),("sofortbanking",6)],
    "Giropay":   [("giropay.de",8),("giropay",6)],
    "Iyzico":    [("iyzipay.com",8),("Iyzipay",8)],
    "PayTR":     [("paytr.com",8),("PayTR",6)],
}

# CSP domain → gateway mapping
CSP_GW: dict[str,str] = {
    "js.stripe.com":"Stripe","stripe.com":"Stripe","checkout.stripe.com":"Stripe",
    "paypal.com":"PayPal","paypalobjects.com":"PayPal",
    "braintreepayments.com":"Braintree","braintreegateway.com":"Braintree",
    "authorize.net":"Authorize.Net","checkoutshopper-live.adyen.com":"Adyen",
    "klarna.com":"Klarna","x.klarnacdn.net":"Klarna",
    "js.afterpay.com":"Afterpay","checkout.razorpay.com":"Razorpay",
    "cdn.worldpay.com":"Worldpay","cdn.checkout.com":"Checkout.com",
    "app.mollie.com":"Mollie","cdn.paddle.com":"Paddle",
    "payments.squareup.com":"Square","pay.google.com":"Google Pay",
    "payments.amazon.com":"Amazon Pay","widget.sezzle.com":"Sezzle",
    "js.recurly.com":"Recurly","js.chargebee.com":"Chargebee",
    "securegw.paytm.in":"Paytm","sdk.cashfree.com":"Cashfree",
    "bluesnap.com":"BlueSnap","secure.networkmerchants.com":"NMI",
    "shop.app":"Shopify Pay","alipay.com":"Alipay",
    "cdn1.affirm.com":"Affirm","xendit.co":"Xendit",
    "sdk.mercadopago.com":"Mercado Pago","iyzipay.com":"Iyzico",
    "dlocalgo.com":"dLocal","hyperpay.com":"HyperPay",
    "moyasar.com":"Moyasar","tap.company":"Tap Payments",
}

# API key regex patterns
API_KEY_PAT: list[tuple[re.Pattern,str]] = [
    (re.compile(r'\bpk_live_[A-Za-z0-9]{20,}\b'),   "Stripe"),
    (re.compile(r'\bpk_test_[A-Za-z0-9]{20,}\b'),   "Stripe"),
    (re.compile(r'paypal[_\-]?client[_\-]?id["\s:=\']+[A-Za-z0-9_\-]{10,}'), "PayPal"),
    (re.compile(r'\bsq0[a-z]{3}-[A-Za-z0-9_\-]{20,}\b'), "Square"),
    (re.compile(r'\brzp_(live|test)_[A-Za-z0-9]{14,}\b'), "Razorpay"),
    (re.compile(r'recurly[_\-]?public[_\-]?key["\s:=\']+[A-Za-z0-9_\-]{8,}'), "Recurly"),
    (re.compile(r'braintree[_\-]?token["\s:=\']+[A-Za-z0-9_\-]{10,}'), "Braintree"),
    (re.compile(r'_affirm_config\s*='), "Affirm"),
    (re.compile(r'mp_public_key["\s:=\']+[A-Za-z0-9_\-]{10,}'), "Mercado Pago"),
    (re.compile(r'xendit[_\-]?public[_\-]?key["\s:=\']+[A-Za-z0-9_\-]{8,}'), "Xendit"),
]

PLT: dict[str,list[tuple[str,int]]] = {
    "Shopify":     [("cdn.shopify.com",5),("myshopify.com",5),("window.Shopify",5),("Shopify.theme",4)],
    "WooCommerce": [("woocommerce/assets",5),("wc_add_to_cart_params",5),("wc_cart_hash_key",4),("wc-ajax",4)],
    "WordPress":   [("wp-emoji-release.min.js",5),('content="WordPress',5),("/wp-includes/js/",4),("/wp-content/themes/",4)],
    "BigCommerce": [("cdn11.bigcommerce.com",6),("BCData",5),("stencil-utils",5)],
    "Magento":     [("MAGE_URLS",6),("Magento_Ui/js",5),("Mage.Cookies",4)],
    "PrestaShop":  [("prestashop.com",5),("PrestaShop",4)],
    "OpenCart":    [("route=product/product",5),("catalog/view/javascript/jquery",5)],
    "Squarespace": [("Y.Squarespace",6),("squarespace.com",5)],
    "Wix":         [("wixstatic.com",6),("wixCode",5),("wix-warmup-data",4)],
    "Webflow":     [("js.webflow.com",6),("w-webflow-badge",5)],
    "Joomla":      [('content="Joomla',6),("/components/com_content",6)],
    "Drupal":      [("drupalSettings",6),("Drupal.settings",6)],
    "Next.js":     [("__NEXT_DATA__",6),("_next/static/chunks",5),("next-head-count",4)],
    "Nuxt.js":     [("__NUXT__",6),("/_nuxt/",5)],
    "Laravel":     [("laravel_session",5),("XSRF-TOKEN",4)],
}

PLT_GROUPS = [
    ["Shopify","WooCommerce","WordPress","BigCommerce","Magento",
     "PrestaShop","OpenCart","Squarespace","Wix","Webflow","Joomla","Drupal","Laravel"],
    ["Next.js","Nuxt.js"],
]

TECH: dict[str,dict[str,list[str]]] = {
    "Analytics": {
        "GA4":     ["gtag/js?id=G-","googletagmanager.com/gtag"],
        "GA UA":   ["ga('send'","analytics.js","UA-"],
        "Hotjar":  ["hotjar.com","hjSiteSettings"],
        "Mixpanel":["mixpanel.com","mixpanel.track"],
        "Clarity": ["clarity.ms","microsoft.clarity"],
        "Plausible":["plausible.io"],
    },
    "Chat": {
        "Intercom": ["intercom.io","window.Intercom"],
        "Zendesk":  ["zendesk.com","zd-messenger"],
        "Crisp":    ["crisp.chat","window.$crisp"],
        "Tawk.to":  ["tawk.to","window.Tawk_API"],
        "Tidio":    ["tidio.co","tidioChatApi"],
        "Drift":    ["drift.com","window.drift"],
    },
    "Email": {
        "Klaviyo":   ["klaviyo.com","_learnq"],
        "Mailchimp": ["mailchimp.com","chimpstatic"],
        "Hubspot":   ["hubspot.com","hs-analytics"],
        "Omnisend":  ["omnisend.com"],
    },
    "CDN": {
        "Cloudflare": ["cdn-cgi/","__cfduid"],
        "Fastly":     ["x-fastly","fastly.net"],
        "CloudFront": ["cloudfront.net"],
        "Bunny CDN":  ["b-cdn.net","bunnycdn"],
        "Akamai":     ["akamaized.net","akamaihd.net"],
        "Vercel":     ["vercel.app","x-vercel"],
    },
}

CAPTCHA_SIGS = [
    "g-recaptcha","grecaptcha","recaptcha/api.js","hcaptcha.com/1/api.js",
    "challenges.cloudflare.com/turnstile","data-sitekey","arkoselabs","funcaptcha",
]

WAF: dict[str,list[str]] = {
    "Cloudflare": ["cf-ray","__cf_bm","cdn-cgi/","server: cloudflare"],
    "Akamai":     ["akamaized.net","ak_bmsc"],
    "Imperva":    ["incapsula","visid_incap","incap_ses"],
    "Sucuri":     ["sucuri.net","sucuri_cloudproxy"],
    "AWS WAF":    ["x-amzn-requestid","x-amz-cf-id"],
    "Fastly":     ["x-fastly-request-id"],
    "Vercel":     ["x-vercel-id","vercel.app"],
    "F5 BIG-IP":  ["TS0","BigIP"],
}

SEC_HEADERS = [
    "strict-transport-security","content-security-policy","x-frame-options",
    "x-content-type-options","x-xss-protection","permissions-policy","referrer-policy",
]

WEBHOOK_PATHS: dict[str,str] = {
    "/webhook/stripe":"Stripe","/stripe/webhook":"Stripe","/wc-api/wc_stripe":"Stripe",
    "/webhook/paypal":"PayPal","/paypal/webhook":"PayPal","/ipn.php":"PayPal",
    "/braintree/webhook":"Braintree","/adyen/webhook":"Adyen",
    "/klarna/webhook":"Klarna","/square/webhook":"Square",
    "/mollie/webhook":"Mollie","/razorpay/webhook":"Razorpay",
    "/paddle/webhook":"Paddle",
}

WELL_KNOWN: dict[str,str] = {
    "/.well-known/apple-developer-merchantid-domain-association":"Apple Pay",
    "/.well-known/pay-web":"Google Pay",
    "/apple-pay-merchant-validation":"Apple Pay",
}

DNS_SUBS = ["pay","checkout","payment","payments","billing",
            "stripe","paypal","shop","secure","order","cart"]

CNAME_GW: dict[str,str] = {
    "stripe.com":"Stripe","paypal.com":"PayPal",
    "braintreepayments.com":"Braintree","authorize.net":"Authorize.Net",
    "squareup.com":"Square","adyen.com":"Adyen",
    "checkout.com":"Checkout.com","klarna.com":"Klarna",
    "afterpay.com":"Afterpay","razorpay.com":"Razorpay",
    "worldpay.com":"Worldpay","mollie.com":"Mollie",
    "paddle.com":"Paddle","2checkout.com":"2Checkout",
    "xendit.co":"Xendit","mercadopago.com":"Mercado Pago",
    "alipay.com":"Alipay",
}

PAYMENT_KW = ("checkout","cart","payment","pay","order","billing",
              "stripe","paypal","purchase","buy","shop","basket")

# ══════════════════════════════════════════════
#  3DS / 2D SIGNATURES
#
#  HIGH = genuine 3DS protocol evidence ONLY.
#         Generic Stripe API terms (confirmCardPayment,
#         requires_action, next_action, ThreeDSecure …)
#         appear on ALL Stripe sites — DO NOT put them here.
#
#  MED  = supporting hints that alone are not conclusive.
#
#  TWOD = explicit 2D bypass / non-SCA signals.
# ══════════════════════════════════════════════

# ── Cardinal Commerce (dedicated 3DS service) ──────────────────────
THREEDS_CARDINAL = [
    "songbird.cardinalcommerce.com",
    "Cardinal.setup(",
    "cardinalcommerce.com",
    "dfReferenceId",          # Cardinal device fingerprint token
    "Cardinal.trigger(",
    "Cardinal.on(",
]

# ── 3DS v1 protocol fields ─────────────────────────────────────────
THREEDS_V1 = [
    "pa_req", "PaReq", "pareq",          # payer-auth request
    "acs_url", "ACSUrl", "acsUrl",        # access control server
    "term_url", "TermUrl",                # termination URL
    "enrolled=Y", "enrolled=y",
    "verifyEnrollment",
    "pares", "PARes",                     # payer-auth response
]

# ── 3DS v2 / EMV3DS protocol fields ───────────────────────────────
THREEDS_V2 = [
    "ThreeDSMethodURL", "threeDSMethodURL",
    "threeDS2Challenge",
    "EMV3DS", "emv3ds",
    "deviceChannel",                      # 3DS v2 browser/app channel
    "browserAcceptHeader",                # 3DS v2 browser data
    "threeDSServerTransID",
    "dsTransId", "dsTransID",
    "acsTransId",
    "challengeWindowSize",
    "messageType.*AReq",
    "messageType.*ARes",
    "messageType.*CReq",
    "messageType.*CRes",
]

# ── Provider-specific 3DS calls ────────────────────────────────────
THREEDS_PROVIDER = [
    "adyen.threeDS",
    "adyen-3ds",
    "braintree.threeDSecure",             # Braintree 3DS create
    "threeDSecureParameters",             # Braintree 3DS params
    "liabilityShifted",                   # Braintree post-auth result
    "liabilityShift",
    "cko-3ds",                            # Checkout.com 3DS
    "fingerprintToken",                   # 3DS fingerprint token
    "challenge_required",                 # explicit 3DS challenge
    "transStatus",                        # 3DS transaction status
    "eci_flag",                           # Electronic Commerce Indicator
    "cavv",                               # Cardholder Auth Verification
    "xid_",                               # 3DS v1 transaction ID prefix
]

# ── Stripe Checkout redirect (always enforces 3DS when needed) ─────
THREEDS_STRIPE_REDIRECT = [
    "checkout.stripe.com/c/pay/cs_live",
    "checkout.stripe.com/c/pay/cs_test",
]

# ── Merged HIGH list ───────────────────────────────────────────────
THREEDS_HIGH = (
    THREEDS_CARDINAL +
    THREEDS_V1 +
    THREEDS_V2 +
    THREEDS_PROVIDER
)

# ── MED: supporting hints only (low weight) ────────────────────────
THREEDS_MED = [
    "3dsecure", "3d_secure", "3ds_method",
    "threeDS2", "threeds2", "3ds2",
    "verifiedbyvisa", "securecode", "safekey",   # card-network programs
    "authentication_url", "authenticationUrl",
    "payer_authentication", "payer_auth",
    "authentication_required",
    "ds_transaction", "directory_server",
    "stripe_3ds",
    # Stripe request_three_d_secure = 'any' forces 3DS
    "request_three_d_secure",
]

# ── 2D bypass / explicit non-SCA ──────────────────────────────────
TWOD_SIGS = [
    r"no3ds",
    r"skip_3ds",
    r"bypass_3ds",
    r"3ds=false",
    r"threeds=false",
    r"disable_3ds",
    r"direct_charge",
    r"no_authentication",
    r"direct_post",
    r"moto_payment",
    r"skip_authentication",
    r"three_d_secure.*false",
    r"3ds_required.*false",
    r"\"moto\"",               # MOTO transaction type
    r"payment_method_options.*moto",
    r"request_three_d_secure.*never",   # Stripe: never request 3DS
]

JS_HOOK = """
(function(){
    window.__PAY={};
    ['Stripe','PayPal','braintree','Square','AdyenCheckout',
     'Klarna','Razorpay','Paddle','Mollie','Afterpay','affirm',
     'FlutterwaveCheckout','PaystackPop','MercadoPago'
    ].forEach(function(n){
        var o=window[n];
        try{
            Object.defineProperty(window,n,{
                set:function(v){if(v)window.__PAY[n]=true;window['_'+n]=v;},
                get:function(){return window['_'+n]||o;},
                configurable:true
            });
        }catch(e){}
    });
})();
"""

# ══════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════

async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                uid INTEGER PRIMARY KEY,
                username TEXT, first_name TEXT,
                joined_at TEXT DEFAULT(datetime('now')),
                scan_count INTEGER DEFAULT 0,
                daily_scans INTEGER DEFAULT 0,
                daily_date TEXT DEFAULT '',
                daily_limit INTEGER DEFAULT 20,
                is_banned INTEGER DEFAULT 0,
                is_vip INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS cache (
                domain TEXT PRIMARY KEY,
                result_json TEXT,
                cached_at REAL
            );
            CREATE TABLE IF NOT EXISTS scan_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid INTEGER, domain TEXT,
                gateways TEXT, checkout TEXT,
                scanned_at TEXT DEFAULT(datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS monitors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid INTEGER, domain TEXT,
                interval_h INTEGER DEFAULT 6,
                last_gateways TEXT, last_check TEXT,
                active INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        await db.commit()

async def db_upsert(uid, username, first_name):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users(uid,username,first_name) VALUES(?,?,?)
            ON CONFLICT(uid) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name
        """, (uid, username or "", first_name or ""))
        await db.commit()

async def db_user(uid) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT * FROM users WHERE uid=?", (uid,)) as c:
            row = await c.fetchone()
            if not row: return None
            return dict(zip([d[0] for d in c.description], row))

async def db_check_daily(uid) -> tuple[bool, int]:
    """
    Check daily scan limit.
    Returns (allowed, remaining).
    Resets count at midnight automatically.
    """
    today = date.today().isoformat()
    user  = await db_user(uid)
    if not user: return True, DAILY_LIMIT

    # VIP = no limit
    if user.get("is_vip"): return True, 9999

    limit = user.get("daily_limit", DAILY_LIMIT)

    # Reset if new day
    if user.get("daily_date","") != today:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET daily_scans=0, daily_date=? WHERE uid=?",
                (today, uid)
            )
            await db.commit()
        return True, limit

    used = user.get("daily_scans", 0)
    if used >= limit:
        return False, 0
    return True, limit - used

async def db_inc_daily(uid):
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE users SET
                scan_count=scan_count+1,
                daily_scans=CASE WHEN daily_date=? THEN daily_scans+1 ELSE 1 END,
                daily_date=?
            WHERE uid=?
        """, (today, today, uid))
        await db.commit()

async def db_log(uid, domain, gateways, checkout):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO scan_log(uid,domain,gateways,checkout) VALUES(?,?,?,?)",
            (uid, domain, ",".join(gateways), checkout)
        )
        await db.commit()

async def db_stats() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        tu = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
        ts = (await (await db.execute("SELECT SUM(scan_count) FROM users")).fetchone())[0] or 0
        td = (await (await db.execute("SELECT SUM(daily_scans) FROM users WHERE daily_date=?",
                                       (date.today().isoformat(),))).fetchone())[0] or 0
        bn = (await (await db.execute("SELECT COUNT(*) FROM users WHERE is_banned=1")).fetchone())[0]
        vp = (await (await db.execute("SELECT COUNT(*) FROM users WHERE is_vip=1")).fetchone())[0]
        cs = (await (await db.execute("SELECT COUNT(*) FROM cache")).fetchone())[0]
        mn = (await (await db.execute("SELECT COUNT(*) FROM monitors WHERE active=1")).fetchone())[0]
        return dict(users=tu,total_scans=ts,today_scans=td,banned=bn,vip=vp,cache=cs,monitors=mn)

async def db_all_uids() -> list[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT uid FROM users WHERE is_banned=0") as c:
            return [r[0] for r in await c.fetchall()]

async def db_all_users() -> list[dict]:
    """Return all users with full details, sorted by scan_count desc."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT uid, username, first_name, scan_count, daily_scans, daily_date, "
            "daily_limit, is_banned, is_vip, joined_at FROM users ORDER BY scan_count DESC"
        ) as c:
            cols = [d[0] for d in c.description]
            return [dict(zip(cols, r)) for r in await c.fetchall()]

async def db_set(uid, col, val):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE users SET {col}=? WHERE uid=?", (val, uid))
        await db.commit()

async def db_get_cache(domain) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT result_json,cached_at FROM cache WHERE domain=?", (domain,)) as c:
            row = await c.fetchone()
            if row and time.time()-row[1] < CACHE_TTL:
                return json.loads(row[0])
    return None

async def db_set_cache(domain, res):
    safe = {k:v for k,v in res.items() if k not in ("from_cache","scanned_by")}
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO cache(domain,result_json,cached_at) VALUES(?,?,?)",
            (domain, json.dumps(safe, default=str), time.time())
        )
        await db.commit()

async def db_del_cache(domain):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM cache WHERE domain=?", (domain,))
        await db.commit()

async def db_clear_cache():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM cache"); await db.commit()

async def db_add_monitor(uid, domain, h):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO monitors(uid,domain,interval_h,active) VALUES(?,?,?,1)",
            (uid,domain,h)
        )
        await db.commit()

async def db_rm_monitor(uid, domain):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE monitors SET active=0 WHERE uid=? AND domain=?", (uid,domain))
        await db.commit()

async def db_get_monitors() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT * FROM monitors WHERE active=1") as c:
            rows = await c.fetchall()
            cols = [d[0] for d in c.description]
            return [dict(zip(cols,r)) for r in rows]

async def db_upd_monitor(domain, gw_json):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE monitors SET last_gateways=?,last_check=datetime('now') WHERE domain=?",
            (gw_json, domain)
        )
        await db.commit()

async def get_global_daily_limit() -> int:
    """Get global daily limit from settings table."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key='daily_limit'") as c:
            row = await c.fetchone()
            return int(row[0]) if row else DAILY_LIMIT

async def set_global_daily_limit(n: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES('daily_limit',?)",
            (str(n),)
        )
        # Apply to all non-vip users
        await db.execute("UPDATE users SET daily_limit=? WHERE is_vip=0", (n,))
        await db.commit()

# ══════════════════════════════════════════════
#  HTTP
# ══════════════════════════════════════════════

def norm(u):
    u = u.strip()
    return u if u.startswith(("http://","https://")) else "https://"+u

def hdrs(ua=""):
    return {
        "User-Agent": ua or random.choice(UAS),
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "DNT": "1",
    }

def get_proxy():
    return random.choice(PROXY_LIST) if PROXY_LIST else None

async def fetch(url, ua="") -> tuple[int, str, dict, list]:
    proxy = get_proxy()
    for attempt in [url, url.replace("https://","http://",1)]:
        try:
            async with httpx.AsyncClient(
                verify=False, follow_redirects=True,
                timeout=REQ_TIMEOUT,
                headers=hdrs(ua or random.choice(UAS)),
                proxy=proxy,
            ) as c:
                r = await c.get(attempt)
                if r.status_code < 500:
                    return r.status_code, r.text, dict(r.headers), [str(h.url) for h in r.history]
        except Exception: pass

        if HAS_CURL_CFFI:
            try:
                r = cffi_req.get(
                    attempt, headers=hdrs(random.choice(UAS)),
                    timeout=REQ_TIMEOUT, impersonate=random.choice(CFFI_PROFILES),
                    verify=False,
                    proxies={"https":proxy,"http":proxy} if proxy else None,
                )
                if r.status_code < 500:
                    return r.status_code, r.text, dict(r.headers), []
            except Exception: pass

        try:
            r = requests.get(
                attempt, headers=hdrs(random.choice(UAS)),
                timeout=REQ_TIMEOUT, verify=False, allow_redirects=True,
                proxies={"https":proxy,"http":proxy} if proxy else None,
            )
            if r.status_code < 500:
                return r.status_code, r.text, dict(r.headers), [rr.url for rr in r.history]
        except Exception: pass

    return 0, "", {}, []

async def fetch_js(url, referer):
    try:
        async with httpx.AsyncClient(verify=False, follow_redirects=True,
                                      timeout=JS_TIMEOUT, proxy=get_proxy()) as c:
            r = await c.get(url, headers={**hdrs(),"Referer":referer,"Accept":"*/*"})
            if r.status_code == 200:
                ct = r.headers.get("content-type","").lower()
                if not any(x in ct for x in ("image/","font/","audio/","video/")):
                    return r.text[:500_000] if len(r.text)>10 else ""
    except Exception: pass
    return ""

def get_ip(domain):
    try: return socket.gethostbyname(domain)
    except Exception: return "N/A"

def get_ssl(domain):
    info = {"valid":False,"expiry":"N/A","issuer":"N/A","days_left":None}
    try:
        ctx  = ssl.create_default_context()
        conn = ctx.wrap_socket(socket.create_connection((domain,443),timeout=6), server_hostname=domain)
        cert = conn.getpeercert(); conn.close()
        exp  = cert.get("notAfter","")
        if exp:
            dt   = datetime.strptime(exp,"%b %d %H:%M:%S %Y %Z")
            days = (dt-datetime.utcnow()).days
            info.update(expiry=dt.strftime("%Y-%m-%d"),days_left=days,valid=days>0)
        info["issuer"] = dict(x[0] for x in cert.get("issuer",[])).get("organizationName","Unknown")
    except Exception: pass
    return info

async def fetch_wb(domain):
    async with httpx.AsyncClient(verify=False, timeout=WB_TIMEOUT,
                                  proxy=get_proxy(), follow_redirects=True) as c:
        for path in ["/checkout","/cart","/payment",""]:
            try:
                cdx = await c.get(
                    "http://web.archive.org/cdx/search/cdx",
                    params={"url":domain+path,"output":"json","limit":"1",
                            "fl":"timestamp,original","filter":"statuscode:200","from":"20230101"}
                )
                rows = cdx.json()
                if len(rows)<2: continue
                ts,orig = rows[1]
                snap = await c.get(f"http://web.archive.org/web/{ts}id_/{orig}",
                                   headers=hdrs(random.choice(UAS)))
                if snap.status_code==200 and len(snap.text)>300:
                    return snap.text
            except Exception: pass
    return ""

def probe_dns(domain):
    found = []
    parts = domain.split(".")
    root  = ".".join(parts[-2:]) if len(parts)>=2 else domain
    def probe_one(sub):
        if not HAS_DNS: return []
        hits = []
        try:
            r = dns.resolver.Resolver()
            r.lifetime = r.timeout = 3.0
            for rd in r.resolve(f"{sub}.{root}","CNAME"):
                cname = str(rd.target).rstrip(".")
                for key,gw in CNAME_GW.items():
                    if key in cname and gw not in hits: hits.append(gw)
        except Exception: pass
        return hits
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(probe_one,s):s for s in DNS_SUBS}
        done,_ = fut_wait(list(futs),timeout=8)
        for f in done:
            try:
                for gw in f.result():
                    if gw not in found: found.append(gw)
            except Exception: pass
    return found

async def probe_wk(base):
    found = []
    async with httpx.AsyncClient(verify=False, timeout=5, proxy=get_proxy()) as c:
        for path,gw in WELL_KNOWN.items():
            try:
                r = await c.get(base+path, headers=hdrs(), follow_redirects=False)
                if r.status_code in (200,301,302) and gw not in found:
                    found.append(gw)
            except Exception: pass
    return found

async def probe_webhooks(base):
    found = []
    async def probe(path, gw):
        try:
            async with httpx.AsyncClient(verify=False, timeout=5,
                                          proxy=get_proxy(), follow_redirects=False) as c:
                r = await c.head(base+path, headers=hdrs())
                if r.status_code == 405: return gw
                if r.status_code == 200: return gw
                rp = await c.post(base+path,
                                  headers={**hdrs(),"Content-Type":"application/json"},
                                  content=b"{}")
                if rp.status_code in (400,401,403,422):
                    body = rp.text.lower()
                    if gw.lower() in body or "webhook" in body or "signature" in body:
                        return gw
        except Exception: pass
        return None
    results = await asyncio.gather(*[probe(p,g) for p,g in WEBHOOK_PATHS.items()], return_exceptions=True)
    for r in results:
        if r and isinstance(r,str) and r not in found: found.append(r)
    return found

async def fetch_smaps(js_urls, referer):
    corpus = ""
    async with httpx.AsyncClient(verify=False, timeout=5, proxy=get_proxy()) as c:
        for murl in [u+".map" for u in js_urls[:8]]:
            try:
                r = await c.get(murl, headers={**hdrs(),"Referer":referer})
                if r.status_code==200 and len(r.text)>100:
                    try:
                        sm = json.loads(r.text)
                        corpus += " ".join(str(s) for s in sm.get("sources",[])) + " "
                        corpus += " ".join(str(s) for s in sm.get("sourcesContent",[]) if isinstance(s,str))[:60_000]
                    except Exception: corpus += r.text[:30_000]
            except Exception: pass
    return corpus

async def crawl_payment_pages(base, parsed, html):
    found = []
    visited = {base}
    proxy = get_proxy()

    def links(page_html, page_url):
        soup = BeautifulSoup(page_html,"html.parser")
        out  = []
        for a in soup.find_all("a",href=True):
            href = a["href"].strip()
            text = a.get_text().lower()
            if not href or href.startswith(("mailto:","tel:","javascript:","#")): continue
            if href.startswith("//"): href = "https:"+href
            elif href.startswith("/"): href = f"{parsed.scheme}://{parsed.netloc}{href}"
            elif not href.startswith("http"): href = urljoin(page_url,href)
            if parsed.netloc not in href: continue
            if any(k in href.lower() or k in text for k in PAYMENT_KW): out.append(href)
        return out

    l1 = links(html, base)
    found.extend(l1)
    async with httpx.AsyncClient(verify=False, follow_redirects=True, timeout=8, proxy=proxy) as c:
        for url in l1[:4]:
            if url in visited: continue
            visited.add(url)
            try:
                r = await c.get(url, headers=hdrs(random.choice(UAS)))
                if r.status_code==200 and len(r.text)>200:
                    for lnk in links(r.text, url):
                        if lnk not in found: found.append(lnk)
            except Exception: pass
    return list(dict.fromkeys(found))[:8]

def extract(html, base_url, parsed):
    soup   = BeautifulSoup(html,"html.parser")
    inline = " ".join(t.get_text() for t in soup.find_all("script") if t.get_text())
    data   = " ".join(f"{k}={v}" for t in soup.find_all(True)
                      for k,v in t.attrs.items() if isinstance(v,str))
    forms  = []
    for form in soup.find_all("form"):
        action = form.get("action","").strip()
        if action:
            if action.startswith("//"): action = "https:"+action
            elif action.startswith("/"): action = f"{parsed.scheme}://{parsed.netloc}{action}"
            forms.append(f"form_action={action}")
        for inp in form.find_all("input"):
            n = inp.get("name","")
            if n: forms.append(f"{n}={inp.get('value','')}")
            for a,v in inp.attrs.items():
                if a.startswith("data-"): forms.append(f"{a}={v}")
    iframes  = [f"iframe={t.get('src','')}" for t in soup.find_all(["iframe","embed"]) if t.get("src")]
    preloads = [lk.get("href","") for lk in soup.find_all("link")
                if any(r in " ".join(lk.get("rel",[])).lower()
                       for r in ("preload","prefetch","dns-prefetch","preconnect"))
                and lk.get("href")]
    extra   = " ".join(forms+iframes+preloads)
    js_urls = []
    for tag in soup.find_all("script",src=True):
        src = tag.get("src","").strip()
        if not src or src.startswith("data:"): continue
        if src.startswith("//"): src = "https:"+src
        elif src.startswith("/"): src = f"{parsed.scheme}://{parsed.netloc}{src}"
        elif not src.startswith("http"): src = urljoin(base_url,src)
        if src not in js_urls: js_urls.append(src)
    return f"{html}\n{inline}\n{data}\n{extra}", js_urls

# ══════════════════════════════════════════════
#  DETECTION
# ══════════════════════════════════════════════

def score_sigs(text, sigs):
    lo = text.lower()
    return {n: sum(w for s,w in entries if s.lower() in lo) for n,entries in sigs.items()}

def match_any(text, sigs):
    lo = text.lower()
    return [n for n,pats in sigs.items() if any(p.lower() in lo for p in pats)]

def best_platform(scores):
    out = []
    for group in PLT_GROUPS:
        gs   = {p: scores.get(p,0) for p in group}
        best, bsc = max(gs.items(), key=lambda x: x[1])
        if bsc < PLT_MIN: continue
        out.append(best)
        for n,sc in gs.items():
            if n != best and sc >= PLT_MIN and bsc-sc <= 1: out.append(n)
    done = {p for g in PLT_GROUPS for p in g}
    for n,sc in scores.items():
        if n not in done and sc >= PLT_MIN: out.append(n)
    return list(dict.fromkeys(out))

def scan_api_keys(corpus):
    return list({gw for pat,gw in API_KEY_PAT if pat.search(corpus)})

def parse_csp(rh):
    csp = rh.get("Content-Security-Policy") or rh.get("content-security-policy","")
    if not csp: return []
    found = []
    for token in re.split(r'[\s;]+',csp.lower()):
        token = re.sub(r'^(https?://|\*\.)','',token.strip())
        for domain,gw in CSP_GW.items():
            if domain.lower() in token and gw not in found: found.append(gw)
    return found

def detect_tech(corpus):
    lo = corpus.lower()
    result = {}
    for cat,services in TECH.items():
        detected = [name for name,pats in services.items() if any(p.lower() in lo for p in pats)]
        if detected: result[cat] = detected
    return result

def detect_3ds(corpus, redirect_urls: list[str] = None):
    """
    3DS detection v3 — protocol-level evidence only.

    Scoring:
      Stripe Checkout redirect (cs_live) → +20  (definitive, Stripe enforces 3DS)
      Stripe Checkout redirect (cs_test) → +15
      Cardinal Commerce sig              → +8 each
      3DS v1 protocol field              → +8 each  (pa_req, ACSUrl …)
      3DS v2 protocol field              → +8 each  (EMV3DS, ThreeDSMethodURL …)
      Provider 3DS sig                   → +6 each  (adyen.threeDS, braintree.threeDSecure …)
      MED hint                           → +2 each  (generic 3DS terms)
      2D bypass sig                      → forces 2D regardless of score

    Decision:
      2D sig found           → 2D (No 3DS)
      score >= 16            → 3D Secure ✅ Confirmed
      score 8–15             → 3D Secure ✅ Likely
      score 4–7              → 3D Secure ⚠️ Possible
      score 0–3              → Unknown
    """
    lo   = corpus.lower()
    score = 0
    evid  = []

    def hit(sig, pts, label=None):
        nonlocal score
        score += pts
        tag = label or sig
        if len(evid) < 8: evid.append(tag)

    # ── Stripe Checkout redirect — strongest signal ─────────────────
    if redirect_urls:
        for rurl in redirect_urls:
            if "checkout.stripe.com/c/pay/cs_live" in rurl:
                hit("Stripe Checkout redirect (cs_live)", 20); break
            elif "checkout.stripe.com/c/pay/cs_test" in rurl:
                hit("Stripe Checkout redirect (cs_test)", 15); break

    # Also check corpus for redirect URLs embedded in HTML/JS
    for sig in THREEDS_STRIPE_REDIRECT:
        if sig.lower() in lo and "Stripe Checkout redirect" not in " ".join(evid):
            hit(sig, 15)
            break

    # ── Cardinal Commerce ───────────────────────────────────────────
    for sig in THREEDS_CARDINAL:
        if sig.lower() in lo:
            hit(sig, 8)

    # ── 3DS v1 protocol fields ──────────────────────────────────────
    for sig in THREEDS_V1:
        if sig.lower() in lo:
            hit(sig, 8)

    # ── 3DS v2 / EMV3DS fields ──────────────────────────────────────
    for sig in THREEDS_V2:
        # Some V2 sigs use regex (messageType patterns)
        try:
            if re.search(sig.lower(), lo):
                hit(sig, 8)
        except re.error:
            if sig.lower() in lo:
                hit(sig, 8)

    # ── Provider-specific 3DS ───────────────────────────────────────
    for sig in THREEDS_PROVIDER:
        if sig.lower() in lo:
            hit(sig, 6)

    # ── MED hints (supporting only, low weight) ─────────────────────
    for sig in THREEDS_MED:
        if sig.lower() in lo:
            hit(sig, 2)

    # ── 2D bypass / non-SCA ─────────────────────────────────────────
    twod_sig = None
    for pat in TWOD_SIGS:
        try:
            m = re.search(pat, lo)
            if m:
                twod_sig = pat
                break
        except re.error:
            if pat.lower() in lo:
                twod_sig = pat
                break

    # ── Decision ────────────────────────────────────────────────────
    if twod_sig:
        mode  = "2D"
        label = f"2D (No 3DS)"
    elif score >= 16:
        mode  = "3D"
        label = "3D Secure ✅ Confirmed"
    elif score >= 8:
        mode  = "3D"
        label = "3D Secure ✅ Likely"
    elif score >= 4:
        mode  = "3D"
        label = "3D Secure ⚠️ Possible"
    else:
        mode  = "Unknown"
        label = "Unknown"

    return {
        "mode":     mode,
        "label":    label,
        "score":    score,
        "evidence": list(dict.fromkeys(evid))[:5],
        "twod_sig": twod_sig,
    }

# ══════════════════════════════════════════════
#  BROWSER SCAN
# ══════════════════════════════════════════════

async def browser_scan(base, browser):
    if not browser: return {"corpus":"","gateways":[],"platform":[],"tech":{},"redirects":[]}

    corpus    = []
    itc_gw    = []
    all_redirects = []

    try:
        ctx  = await browser.new_context(
            user_agent=UAS[0], viewport={"width":1280,"height":800},
            java_script_enabled=True, ignore_https_errors=True,
        )
        page = await ctx.new_page()
        if HAS_STEALTH: await stealth_async(page)
        await page.add_init_script(JS_HOOK)

        async def on_req(req):
            for domain,gw in CSP_GW.items():
                if domain in req.url and gw not in itc_gw: itc_gw.append(gw)
            # Detect Stripe 3DS redirect
            if "checkout.stripe.com/c/pay/" in req.url:
                all_redirects.append(req.url)
                if "Stripe" not in itc_gw: itc_gw.append("Stripe")

        page.on("request", on_req)

        for path in ["","/checkout","/cart","/payment"]:
            try:
                resp = await page.goto(base+path, wait_until="networkidle", timeout=PW_TIMEOUT)
                await asyncio.sleep(1.5)
                await page.evaluate("window.scrollTo(0,document.body.scrollHeight)")
                await asyncio.sleep(0.5)

                for sel in ["button[class*='checkout']","a[href*='/checkout']",
                             "button[class*='pay']","[data-testid*='checkout']"]:
                    try:
                        el = page.locator(sel).first
                        if await el.count()>0:
                            await el.click(timeout=2000)
                            await asyncio.sleep(1.5)
                            break
                    except Exception: pass

                html = await page.content()
                corpus.append(html)

                # JS hook results
                hooks = await page.evaluate("()=>window.__PAY||{}")
                if hooks:
                    corpus.append(json.dumps(hooks))
                    HMAP = {"Stripe":"Stripe","PayPal":"PayPal","braintree":"Braintree",
                            "Square":"Square","AdyenCheckout":"Adyen","Klarna":"Klarna",
                            "Razorpay":"Razorpay","Paddle":"Paddle","affirm":"Affirm",
                            "MercadoPago":"Mercado Pago"}
                    for k in hooks:
                        for kw,gw in HMAP.items():
                            if kw.lower() in k.lower() and gw not in itc_gw: itc_gw.append(gw)

                # Window globals
                win = await page.evaluate("""()=>{
                    const K=['Stripe','PayPal','braintree','Square','AdyenCheckout',
                        'Klarna','Razorpay','Paddle','affirm','MercadoPago',
                        'STRIPE_KEY','PAYPAL_CLIENT_ID'];
                    const o={};
                    K.forEach(k=>{if(window[k]!==undefined)try{o[k]=String(window[k]).substring(0,400);}catch(e){}});
                    return o;
                }""")
                if win: corpus.append(json.dumps(win))

            except Exception as e: log.debug("PW %s%s: %s",base,path,e)

        await ctx.close()

    except Exception as e: log.debug("browser_scan %s: %s",base,e)

    corp = "\n".join(corpus)
    gws  = list(dict.fromkeys(itc_gw + scan_api_keys(corp)))
    return {"corpus":corp,"gateways":gws,"platform":best_platform(score_sigs(corp,PLT)),
            "tech":detect_tech(corp),"redirects":all_redirects}

# ══════════════════════════════════════════════
#  CORE SCANNER
# ══════════════════════════════════════════════

async def scan(raw, uid, force_fresh=False, scanned_by=""):
    url    = norm(raw)
    parsed = urlparse(url)
    domain = parsed.netloc or url
    base   = f"{parsed.scheme}://{parsed.netloc}"

    if not force_fresh:
        cached = await db_get_cache(domain)
        if cached:
            cached.update(from_cache=True, scanned_by=scanned_by)
            return cached

    res = dict(
        url=url, domain=domain, status="N/A", ip="N/A",
        waf=[], captcha=False, gateways=[], platform=[],
        tech={}, wb=False, error=None, response_ms=None,
        ssl={"valid":False,"expiry":"N/A","issuer":"N/A","days_left":None},
        server="Unknown", sec_headers={}, redirects=[],
        gw_src={}, used_pw=False, from_cache=False,
        scanned_by=scanned_by,
        checkout={"mode":"Unknown","score":0,"evidence":[]},
    )

    c    = lambda: is_cancelled(uid)
    loop = asyncio.get_event_loop()

    # All parallel from t=0
    ip_fut  = loop.run_in_executor(None, get_ip, domain)
    ssl_fut = loop.run_in_executor(None, get_ssl, domain)
    dns_fut = loop.run_in_executor(None, probe_dns, domain)
    wb_task = asyncio.create_task(fetch_wb(domain))
    wk_task = asyncio.create_task(probe_wk(base))
    wh_task = asyncio.create_task(probe_webhooks(base))

    t0 = time.time()
    status, html, resp_headers, redirects = await fetch(url, UAS[0])
    res["response_ms"] = int((time.time()-t0)*1000)
    res["redirects"]   = redirects

    # Check redirects for Stripe gateway
    all_redirect_urls = redirects.copy()
    redirect_str = " ".join(redirects)
    if "checkout.stripe.com" in redirect_str or "stripe.com/pay" in redirect_str:
        if "Stripe" not in res["gateways"]:
            res["gateways"].append("Stripe")

    wb_html = ""
    if status in (403,429,503,0):
        wb_html = await wb_task
        if wb_html: res["wb"] = True
        if status == 0:
            res["error"] = "Unreachable" + (" — Wayback ✓" if wb_html else "")
            if not wb_html:
                res["ip"] = await ip_fut
                res["ssl"] = await ssl_fut
                for t in [wk_task,wh_task]: t.cancel()
                return res
    else:
        wb_task.cancel()

    res["status"]      = status
    csp_gw             = parse_csp(resp_headers)
    hdr_blob           = " ".join(f"{k.lower()}:{v.lower()}" for k,v in resp_headers.items())
    res["server"]      = " ".join(v for k in ("server","x-powered-by")
                                   for v in [resp_headers.get(k,resp_headers.get(k.title(),""))] if v) or "Unknown"
    res["sec_headers"] = {h: h in {k.lower() for k in resp_headers} for h in SEC_HEADERS}

    if c():
        res["error"] = "Cancelled"
        for t in [wk_task,wh_task]: t.cancel()
        return res

    corpus   = ""
    js_urls  = []

    def absorb(ht, pu):
        nonlocal corpus, js_urls
        text, new_js = extract(ht, pu, parsed)
        corpus += text+"\n"
        for u2 in new_js:
            if u2 not in js_urls: js_urls.append(u2)

    if html: absorb(html, url)
    if wb_html: absorb(wb_html, url)

    discovered = await crawl_payment_pages(base, parsed, html or "")
    FIXED = ["/checkout","/cart","/shop","/payment","/pay","/order",
             "/?wc-ajax=get_refreshed_fragments","/index.php?route=checkout/cart","/cart.js"]
    all_extra = list(dict.fromkeys(discovered + [base+p for p in FIXED]))

    async def fetch_extra(eu):
        if c(): return ""
        s,t,_,rdrs = await fetch(eu, random.choice(UAS))
        all_redirect_urls.extend(rdrs)
        return t if s==200 and len(t)>200 else ""

    extra_res = await asyncio.gather(*[fetch_extra(eu) for eu in all_extra], return_exceptions=True)
    for txt in extra_res:
        if isinstance(txt,str) and txt: absorb(txt, url)

    if c():
        res["error"] = "Cancelled"
        for t in [wk_task,wh_task]: t.cancel()
        return res

    PAY_KW = ("stripe","paypal","braintree","adyen","klarna","checkout","square","authorize")
    prio  = [u for u in js_urls if any(k in u.lower() for k in PAY_KW)]
    rest  = [u for u in js_urls if u not in prio]
    batch = (prio+rest)[:MAX_JS]

    js_res = await asyncio.gather(*[fetch_js(u,url) for u in batch], return_exceptions=True)
    ext_js = "\n".join(r for r in js_res if isinstance(r,str) and r)

    smaps = await fetch_smaps(batch, url)

    # Browser scan inside pool
    pw_res = {"corpus":"","gateways":[],"platform":[],"tech":{},"redirects":[]}
    if _pool and _pool.ok():
        try:
            async with _pool.acquire() as browser:
                if browser:
                    pw_res = await browser_scan(base, browser)
                    res["used_pw"] = bool(pw_res["corpus"])
                    all_redirect_urls.extend(pw_res.get("redirects",[]))
        except Exception as e: log.debug("pool: %s",e)

    wk_gw = await wk_task
    wh_gw = await wh_task
    dn_gw = await dns_fut
    res["ip"]  = await ip_fut
    res["ssl"] = await ssl_fut

    full = "\n".join([corpus, ext_js, smaps, hdr_blob, pw_res.get("corpus",""),
                      " ".join(all_redirect_urls)])

    api_gw = scan_api_keys(full)

    res["waf"]     = list(dict.fromkeys(match_any(hdr_blob,WAF)+match_any(full,WAF)))
    res["captcha"] = any(p in full.lower() for p in CAPTCHA_SIGS)
    res["tech"]    = detect_tech(full)
    for cat,items in pw_res.get("tech",{}).items():
        existing = res["tech"].setdefault(cat,[])
        for item in items:
            if item not in existing: existing.append(item)

    gw_sc = score_sigs(full, GW)
    confirmed = set(csp_gw+api_gw+wh_gw+wk_gw+dn_gw+pw_res.get("gateways",[]))

    # Redirect-detected gateways are confirmed
    rdr_str = " ".join(all_redirect_urls)
    if "checkout.stripe.com" in rdr_str or "stripe.com/pay" in rdr_str:
        confirmed.add("Stripe")
    if "paypal.com/checkoutnow" in rdr_str or "paypal.com/cgi-bin" in rdr_str:
        confirmed.add("PayPal")

    for gw in confirmed:
        gw_sc[gw] = max(gw_sc.get(gw,0), GW_THRESHOLD*3)

    gw_list = [n for n,sc in gw_sc.items() if sc >= GW_THRESHOLD]
    # Merge with any already detected from redirects
    for g in res["gateways"]:
        if g not in gw_list: gw_list.insert(0, g)
    res["gateways"] = gw_list

    plt_sc = score_sigs(full, PLT)
    res["platform"] = best_platform(plt_sc)
    for p in pw_res.get("platform",[]):
        if p not in res["platform"]: res["platform"].append(p)

    res["checkout"] = detect_3ds(full, all_redirect_urls)

    res["gw_src"] = {
        "csp":csp_gw,"api":api_gw,"webhook":wh_gw,
        "wellknown":wk_gw,"dns":dn_gw,"browser":pw_res.get("gateways",[]),
        "redirect":[g for g in confirmed if any(g.lower() in u for u in all_redirect_urls)],
    }

    log.info("✓ %s GW:%s PLT:%s CS:%s PW:%s",
             domain,res["gateways"],res["platform"],res["checkout"]["mode"],res["used_pw"])

    await db_set_cache(domain, res)
    return res

# ══════════════════════════════════════════════
#  MONITORING
# ══════════════════════════════════════════════

async def monitor_loop(app, uid, domain, interval_h):
    log.info("Monitor: %s every %dh uid=%d", domain, interval_h, uid)
    last = set()
    while True:
        await asyncio.sleep(interval_h*3600)
        monitors = await db_get_monitors()
        if not any(m["domain"]==domain and m["uid"]==uid for m in monitors): break
        try:
            r   = await scan(f"https://{domain}", uid=0, force_fresh=True)
            cur = set(r.get("gateways",[]))
            added   = cur-last
            removed = last-cur
            if last and (added or removed):
                parts = [f"🔔 *{domain}*"]
                if added:   parts.append(f"✅ New: {', '.join(added)}")
                if removed: parts.append(f"❌ Gone: {', '.join(removed)}")
                try: await app.bot.send_message(uid,"\n".join(parts),parse_mode="Markdown")
                except Exception: pass
            elif not last:
                gw_str = ", ".join(cur) if cur else "None"
                try: await app.bot.send_message(uid,f"✅ Monitor: {domain}\nGW: `{gw_str}`",parse_mode="Markdown")
                except Exception: pass
            last = cur
            await db_upd_monitor(domain, json.dumps(list(cur)))
        except Exception as e: log.debug("monitor %s: %s",domain,e)

# ══════════════════════════════════════════════
#  PHASE-BASED LOADER
# ══════════════════════════════════════════════

class ScanProgress:
    """Track scan phases to show real progress."""
    def __init__(self):
        self.phase_pct = 0
        self.phase_txt = "🌐 Connecting..."

    def set(self, pct, txt):
        self.phase_pct = pct
        self.phase_txt = txt

_scan_progress: dict[int, ScanProgress] = {}

async def phase_loader(msg, uid, domain, total, idx, stop, progress: ScanProgress):
    fi, bar_len = 0, 14
    multi = f"[{idx}/{total}] " if total>1 else ""
    while not stop.is_set():
        if is_cancelled(uid):
            try: await msg.edit_text(f"❌ *Cancelled* — `{domain}`", parse_mode="Markdown")
            except Exception: pass
            return
        pct  = progress.phase_pct
        txt  = progress.phase_txt
        bar  = "█"*int(bar_len*pct/100)+"░"*(bar_len-int(bar_len*pct/100))
        try:
            await msg.edit_text(
                f"{FRAMES[fi%len(FRAMES)]} *{multi}Scanning* `{domain}`\n\n"
                f"`[{bar}]` {pct}%\n\n"
                f"_{txt}_",
                parse_mode="Markdown",
            )
        except Exception: pass
        fi += 1
        await asyncio.sleep(1.2)

# ══════════════════════════════════════════════
#  FORMAT  (clean, concise)
# ══════════════════════════════════════════════

SE = {200:"🟢",201:"🟢",301:"🔀",302:"🔀",400:"🟡",403:"🔴",404:"🟡",429:"🟠",500:"⛔",503:"⛔"}

def fmt(r):
    if r.get("error")=="Cancelled":
        return f"❌ Cancelled — {r['domain']}"

    if r["error"] and not r["wb"] and not r["gateways"]:
        return (
            "◇ • SITE CHECKER • ◇\n"
            "─────────────────────\n"
            f"Site   : {r['domain']}\n"
            f"IP     : {r['ip']}\n"
            f"Error  : {r['error']}\n"
            "─────────────────────\n"
            f"Bot By : {r.get('scanned_by') or BOT_AUTHOR}"
        )

    gw  = " | ".join(f"🔥{g}" for g in r["gateways"]) if r["gateways"] else "❌ None"
    plt = ", ".join(r["platform"]) if r["platform"] else "Unknown"
    waf = ", ".join(r["waf"])      if r["waf"]      else "None"
    cap = "✅" if r["captcha"] else "❌"
    st  = r["status"]
    ms  = f"({r['response_ms']}ms)" if r["response_ms"] else ""
    srv = r.get("server","Unknown")
    rdr = f"{len(r.get('redirects',[]))} hop(s)" if r.get("redirects") else "None"

    # Checkout
    cs      = r.get("checkout",{})
    mode    = cs.get("mode","Unknown")
    label   = cs.get("label","Unknown")
    evid    = cs.get("evidence",[])
    twod_sig= cs.get("twod_sig","")
    if mode=="3D":
        cs_str = f"🔒 {label}"
        if evid: cs_str += f" [{evid[0]}]"
    elif mode=="2D":
        cs_str = f"⚠️ 2D (No 3DS)"
        if twod_sig: cs_str += f" [{twod_sig}]"
    else:
        cs_str = "❓ Unknown"

    # SSL
    ssl_ = r.get("ssl",{})
    if ssl_.get("expiry","N/A")!="N/A":
        d    = ssl_["days_left"]
        icon = "✅" if d and d>30 else ("⚠️" if d and d>0 else "❌")
        ssl_str = f"{icon} {ssl_['expiry']} ({d}d) — {ssl_['issuer']}"
    else:
        ssl_str = "N/A"

    # SecHeaders — short
    sh = r.get("sec_headers",{})
    SHORT = {"strict-transport-security":"HSTS","content-security-policy":"CSP",
             "x-frame-options":"XFO","x-content-type-options":"XCTO",
             "x-xss-protection":"XSS","permissions-policy":"PP","referrer-policy":"RP"}
    have    = [SHORT[h] for h,ok in sh.items() if ok]
    missing = [SHORT[h] for h,ok in sh.items() if not ok]
    sec_str = ""
    if have:    sec_str += "✅"+",".join(have)
    if missing: sec_str += (" " if have else "")+"❌"+",".join(missing)
    sec_str = sec_str or "N/A"

    # Evidence
    src   = r.get("gw_src",{})
    evids = []
    for key,label in [("csp","CSP"),("api","Key"),("browser","PW"),
                       ("webhook","Hook"),("wellknown","WK"),("dns","DNS"),("redirect","Redirect")]:
        vals = src.get(key,[])
        if vals: evids.append(f"{label}:{','.join(vals[:2])}")
    evid_line = ("\n▒➳ Evidence  : "+" | ".join(evids)) if evids else ""

    # Tech — one line
    tech = r.get("tech",{})
    tech_parts = []
    for cat,items in tech.items():
        if items: tech_parts.append(f"{cat}: {', '.join(items[:2])}")
    tech_line = ("\n▒➳ Tech      : "+"\n▒➳            ".join(tech_parts)) if tech_parts else ""

    # Badges
    badges = []
    if r["wb"]:             badges.append("WB")
    if r.get("used_pw"):    badges.append("PW")
    if r.get("from_cache"): badges.append("Cache")
    badge = " ["+",".join(badges)+"]" if badges else ""

    # Tags
    tags  = [f"#{g.replace(' ','').replace('/','').replace('.','')}" for g in r["gateways"]]
    if not r["gateways"]: tags.append("#NoGW")
    tags += [f"#{p.replace(' ','').replace('.','')}" for p in r["platform"]]
    tags += [f"#{''.join(w.split()[0] for w in r['waf'])}"] if r["waf"] else ["#NoCF"]
    tags.append("#Cap" if r["captcha"] else "#NoCap")
    tags.append(f"#{mode}" if mode in ("2D","3D") else "#CS?")

    return (
        "◇ • ¡SITE CHECKER! • ◇\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"▒➳ Site      : {r['domain']}\n"
        f"▒➳ IP        : {r['ip']}\n"
        f"▒➳ Status    : {SE.get(st,'⚪')} {st} {ms}{badge}\n"
        f"▒➳ Redirect  : {rdr}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"▒➳ Gateways  : {gw}\n"
        f"▒➳ Checkout  : {cs_str}\n"
        f"▒➳ Platform  : {plt}\n"
        f"▒➳ Server    : {srv}\n"
        f"▒➳ WAF/CDN   : {waf}\n"
        f"▒➳ Captcha   : {cap}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"▒➳ SSL       : {ssl_str}\n"
        f"▒➳ SecHdrs   : {sec_str}"
        f"{evid_line}"
        f"{tech_line}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{' '.join(tags[:8])}\n"
        f"Bot By : {r.get('scanned_by') or BOT_AUTHOR}"
    )

# ══════════════════════════════════════════════
#  EXPORT
# ══════════════════════════════════════════════

def export_txt(results):
    lines = [f"Site Checker v10 — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"]
    for r in results:
        cs = r.get("checkout",{})
        ssl_ = r.get("ssl",{})
        lines += ["="*45,
            f"Site    : {r['domain']}",
            f"IP      : {r.get('ip','N/A')}",
            f"Status  : {r.get('status','N/A')}",
            f"Gateways: {', '.join(r['gateways']) if r['gateways'] else 'None'}",
            f"Checkout: {cs.get('mode','Unknown')} (score:{cs.get('score',0)})",
            f"Platform: {', '.join(r['platform']) if r['platform'] else 'Unknown'}",
            f"WAF/CDN : {', '.join(r.get('waf',[])) or 'None'}",
            f"SSL     : {ssl_.get('expiry','N/A')} — {ssl_.get('issuer','N/A')}",""]
    return "\n".join(lines)

def export_csv(results):
    out = io.StringIO()
    w   = csv.DictWriter(out, fieldnames=[
        "domain","ip","status","gateways","checkout","platform",
        "waf","captcha","ssl_expiry","ssl_issuer","server","response_ms"])
    w.writeheader()
    for r in results:
        ssl_ = r.get("ssl",{}); cs = r.get("checkout",{})
        w.writerow({
            "domain":r["domain"],"ip":r.get("ip","N/A"),"status":r.get("status","N/A"),
            "gateways":"|".join(r.get("gateways",[])),
            "checkout":cs.get("mode","Unknown"),
            "platform":"|".join(r.get("platform",[])),
            "waf":"|".join(r.get("waf",[])),
            "captcha":"Yes" if r.get("captcha") else "No",
            "ssl_expiry":ssl_.get("expiry","N/A"),
            "ssl_issuer":ssl_.get("issuer","N/A"),
            "server":r.get("server","Unknown"),
            "response_ms":r.get("response_ms",""),
        })
    return out.getvalue()

def export_json(results):
    return json.dumps(results, indent=2, default=str)

# ══════════════════════════════════════════════
#  SCAN RUNNER
# ══════════════════════════════════════════════

async def do_scan(uid, raw, reply_fn, idx=1, total=1, force_fresh=False, scanned_by=""):
    domain = urlparse(norm(raw)).netloc or raw

    # Phase-based progress
    prog = ScanProgress()
    _scan_progress[uid] = prog

    lmsg = await reply_fn(
        f"⠋ *[{idx}/{total}] Scanning* `{domain}`\n\n"
        "`[░░░░░░░░░░░░░░]` 0%\n\n_🌐 Connecting..._",
        parse_mode="Markdown",
    )

    stop = asyncio.Event()
    anim = asyncio.create_task(phase_loader(lmsg, uid, domain, total, idx, stop, prog))

    # Update progress phases during scan
    async def run_scan():
        prog.set(5, "🌐 Connecting...")
        result = await scan(raw, uid, force_fresh, scanned_by)
        return result

    # Simulate phase updates alongside scan
    async def update_phases():
        phases = [
            (2, 15, "📄 Fetching page..."),
            (4, 30, "🔍 Scanning JS..."),
            (8, 45, "🗂 Extra pages..."),
            (12, 60, "🌐 Browser scan..."),
            (16, 75, "🔐 SSL + Headers..."),
            (20, 88, "🧬 DNS + Webhooks..."),
            (25, 95, "📊 Analyzing..."),
        ]
        for delay, pct, txt in phases:
            await asyncio.sleep(delay)
            if not stop.is_set():
                prog.set(pct, txt)

    scan_task   = asyncio.create_task(run_scan())
    phases_task = asyncio.create_task(update_phases())

    result = await scan_task
    phases_task.cancel()

    stop.set()
    await anim
    _scan_progress.pop(uid, None)

    try: await lmsg.delete()
    except Exception: pass
    return result

def save_res(uid, res):
    _last_results[uid].insert(0, res)
    _last_results[uid] = _last_results[uid][:20]

async def run_scans(uid, urls, reply_fn, force_fresh=False, scanned_by=""):
    set_scanning(uid, True)
    try:
        for idx, raw in enumerate(urls,1):
            if is_cancelled(uid): await reply_fn("❌ Scan cancelled."); break

            res = await do_scan(uid, raw, reply_fn, idx, len(urls), force_fresh, scanned_by)
            if not res or res.get("error")=="Cancelled":
                await reply_fn("❌ Scan cancelled."); break

            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Rescan", callback_data=f"rs:{raw}"),
                InlineKeyboardButton("📊 Export", callback_data=f"ex:{raw}"),
            ]])
            await reply_fn(f"```\n{fmt(res)}\n```", parse_mode="Markdown", reply_markup=kb)
            save_res(uid, res)
            await db_inc_daily(uid)
            await db_log(uid, res["domain"], res["gateways"], res["checkout"]["mode"])
    finally:
        set_scanning(uid, False)

# ══════════════════════════════════════════════
#  TELEGRAM HANDLERS
# ══════════════════════════════════════════════

async def reg(update):
    u = update.effective_user
    await db_upsert(u.id, u.username, u.first_name)
    # Set global daily limit for new user
    limit = await get_global_daily_limit()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET daily_limit=? WHERE uid=? AND is_vip=0",
            (limit, u.id)
        )
        await db.commit()

def dname(user):
    return f"@{user.username}" if user.username else (user.first_name or "Unknown")

async def banned(uid):
    u = await db_user(uid)
    return bool(u and u.get("is_banned"))

def is_admin(uid): return uid in ADMIN_IDS

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await reg(update)
    uid = update.effective_user.id
    if await banned(uid): await update.message.reply_text("🚫 Banned."); return
    limit = await get_global_daily_limit()
    pw = "✅ Browser" if (HAS_PLAYWRIGHT and _pool and _pool.ok()) else "⚠️ No Browser"
    await update.message.reply_text(
        f"👾 *Site Checker v10*  —  _{BOT_AUTHOR}_\n\n"
        f"{pw} | {'✅ cffi' if HAS_CURL_CFFI else '⚠️ No cffi'} | {'✅ DNS' if HAS_DNS else '⚠️ No DNS'}\n"
        f"💳 *{len(GW)}+ gateways*\n"
        f"📊 Daily limit: *{limit} scans/day*\n\n"
        "📌 *Commands:*\n"
        "`/check <url>` — Scan\n"
        "`/fresh <url>` — Force rescan\n"
        "`/bulk` — Upload .txt file\n"
        "`/last` — Last results\n"
        "`/export [txt|csv|json]` — Export\n"
        "`/monitor <url> [hours]` — Monitor\n"
        "`/unmonitor <url>` — Stop\n"
        "`/cancel` — Cancel scan\n"
        "`/help` — Help\n\n"
        "💡 URL တိုက်ရိုက်ပို့လည်း ရတယ်!",
        parse_mode="Markdown",
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    limit = await get_global_daily_limit()
    await update.message.reply_text(
        "📖 *Help — v10*\n\n"
        "*Detection layers:*\n"
        "• Browser pool + JS hooking\n"
        "• Redirect URL analysis\n"
        "• CSP header parsing\n"
        "• API key regex\n"
        "• Webhook probing\n"
        "• DNS + .well-known\n"
        "• Source maps\n"
        "• Wayback fallback\n"
        "• 2D/3D Secure detection\n\n"
        "*3D detection includes:*\n"
        "Stripe redirect (cs_live), Cardinal,\n"
        "Adyen 3DS2, Braintree 3DS, SCA\n\n"
        f"Daily limit: {limit} scans/day\n"
        f"Cache TTL: {CACHE_TTL//3600}h",
        parse_mode="Markdown",
    )

async def _check_limits(update, uid) -> bool:
    """Returns True if OK to scan."""
    if await banned(uid):
        await update.message.reply_text("🚫 Banned.")
        return False
    if is_scanning(uid):
        await update.message.reply_text("⚠️ Scan run နေတယ်။ /cancel")
        return False
    ok, remaining = await db_check_daily(uid)
    if not ok:
        limit = await get_global_daily_limit()
        await update.message.reply_text(
            f"❌ Daily limit reached ({limit} scans/day)\n"
            "မနက်ဖြန် reset ဖြစ်မယ် 🌅"
        )
        return False
    return True

async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await reg(update)
    uid = update.effective_user.id
    if not await _check_limits(update, uid): return
    if not ctx.args: await update.message.reply_text("Usage: `/check <url>`", parse_mode="Markdown"); return
    await run_scans(uid, [" ".join(ctx.args)], update.message.reply_text, scanned_by=dname(update.effective_user))

async def cmd_fresh(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await reg(update)
    uid = update.effective_user.id
    if not await _check_limits(update, uid): return
    if not ctx.args: await update.message.reply_text("Usage: `/fresh <url>`", parse_mode="Markdown"); return
    domain = urlparse(norm(" ".join(ctx.args))).netloc
    if domain: await db_del_cache(domain)
    await run_scans(uid, [" ".join(ctx.args)], update.message.reply_text,
                    force_fresh=True, scanned_by=dname(update.effective_user))

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_scanning(uid): request_cancel(uid); await update.message.reply_text("⏹ Cancelling...")
    else: await update.message.reply_text("ℹ️ Run နေတဲ့ scan မရှိဘူး။")

async def cmd_last(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    results = _last_results.get(uid,[])
    if not results: await update.message.reply_text("📭 မရှိသေးဘူး。"); return
    lines = ["📋 *Last Scans:*\n"]
    for i,r in enumerate(results[:10],1):
        gw  = ", ".join(r["gateways"]) if r["gateways"] else "None"
        cs  = r.get("checkout",{}).get("mode","?")
        lines.append(f"`{i}.` `{r['domain']}`\n   GW:`{gw}` | `{cs}` | `{r['status']}`\n")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    results = _last_results.get(uid,[])
    if not results: await update.message.reply_text("📭 No results."); return
    fmt_arg = (ctx.args[0] if ctx.args else "txt").lower()
    if fmt_arg=="csv":  content,fn = export_csv(results),  f"scan_{int(time.time())}.csv"
    elif fmt_arg=="json": content,fn = export_json(results),f"scan_{int(time.time())}.json"
    else:               content,fn = export_txt(results),  f"scan_{int(time.time())}.txt"
    bio = io.BytesIO(content.encode()); bio.name = fn
    await update.message.reply_document(document=bio, filename=fn,
                                         caption=f"📊 {len(results)} results — {fmt_arg.upper()}")

async def cmd_bulk(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"📎 *Bulk Scan*\n\n.txt file upload လုပ်ပါ\n"
        f"(တစ်ကြောင်းတစ်ခု URL — max {MAX_URLS_FILE})",
        parse_mode="Markdown",
    )

async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await reg(update)
    uid = update.effective_user.id
    if not await _check_limits(update, uid): return
    doc = update.message.document
    if not doc or not doc.file_name.endswith(".txt"):
        await update.message.reply_text("⚠️ .txt ပဲ လက်ခံတယ်。"); return
    file = await ctx.bot.get_file(doc.file_id)
    bio  = io.BytesIO(); await file.download_to_memory(bio)
    urls = [l.strip() for l in bio.getvalue().decode("utf-8","ignore").splitlines()
            if l.strip() and "." in l and len(l.strip())>3]
    if not urls: await update.message.reply_text("⚠️ URL မတွေ့ဘူး。"); return
    if len(urls)>MAX_URLS_FILE:
        await update.message.reply_text(f"⚠️ ပထမ {MAX_URLS_FILE} ခုပဲ.")
        urls = urls[:MAX_URLS_FILE]
    await update.message.reply_text(f"📎 {len(urls)} URLs — scanning...", parse_mode="Markdown")
    await run_scans(uid, urls, update.message.reply_text, scanned_by=dname(update.effective_user))

async def cmd_monitor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if await banned(uid): return
    if len(ctx.args or [])<1:
        await update.message.reply_text("Usage: `/monitor <domain> [hours]`", parse_mode="Markdown"); return
    domain = ctx.args[0].strip().lstrip("https://").lstrip("http://").split("/")[0]
    h      = int(ctx.args[1]) if len(ctx.args)>1 and ctx.args[1].isdigit() else 6
    h      = max(1,min(h,24))
    await db_add_monitor(uid,domain,h)
    key = f"{uid}:{domain}"
    if key not in _monitors:
        task = asyncio.create_task(monitor_loop(ctx.application,uid,domain,h))
        _monitors[key] = {"task":task}
    await update.message.reply_text(f"🔔 Monitor: `{domain}` every `{h}h`", parse_mode="Markdown")

async def cmd_unmonitor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not ctx.args: await update.message.reply_text("Usage: `/unmonitor <domain>`", parse_mode="Markdown"); return
    domain = ctx.args[0].strip().lstrip("https://").lstrip("http://").split("/")[0]
    await db_rm_monitor(uid,domain)
    key = f"{uid}:{domain}"
    if key in _monitors: _monitors[key]["task"].cancel(); del _monitors[key]
    await update.message.reply_text(f"✅ Stopped: `{domain}`", parse_mode="Markdown")

async def on_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await reg(update)
    uid  = update.effective_user.id
    if await banned(uid): return
    text = (update.message.text or "").strip()
    if not text: return
    if not await _check_limits(update, uid): return

    urls = [u.strip() for u in re.split(r"[\n,]+",text)
            if u.strip() and "." in u and len(u.strip())>3]
    if not urls:
        parts = text.split()
        urls  = [p for p in parts if "." in p and len(p)>3]
    if not urls: await update.message.reply_text("⚠️ Valid URL မတွေ့ဘူး。"); return
    if len(urls)>MAX_URLS_MSG:
        await update.message.reply_text(f"⚠️ ပထမ {MAX_URLS_MSG} ခုပဲ scan မယ်。")
        urls = urls[:MAX_URLS_MSG]
    await run_scans(uid, urls, update.message.reply_text, scanned_by=dname(update.effective_user))

# ── Admin ─────────────────────────────────────

async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): await update.message.reply_text("⛔"); return
    s     = await db_stats()
    limit = await get_global_daily_limit()
    kb    = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats",       callback_data="adm:stats"),
         InlineKeyboardButton("👥 Users",        callback_data="adm:users")],
        [InlineKeyboardButton("📢 Broadcast",   callback_data="adm:broadcast"),
         InlineKeyboardButton("🔔 Monitors",    callback_data="adm:monitors")],
        [InlineKeyboardButton("🗑 Clear Cache", callback_data="adm:clearcache"),
         InlineKeyboardButton("⚙️ Help",        callback_data="adm:help")],
    ])
    await update.message.reply_text(
        f"🛡 *Admin Panel v10*\n\n"
        f"👥 Users: `{s['users']}`\n"
        f"🔍 Total scans: `{s['total_scans']}`\n"
        f"📅 Today: `{s['today_scans']}`\n"
        f"💾 Cache: `{s['cache']}`\n"
        f"🔔 Monitors: `{s['monitors']}`\n"
        f"🚫 Banned: `{s['banned']}`\n"
        f"⭐ VIP: `{s['vip']}`\n\n"
        f"📊 Daily limit: `{limit} scans/day`\n"
        f"_(change: /setdaily <n>)_",
        parse_mode="Markdown", reply_markup=kb,
    )

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    s     = await db_stats()
    limit = await get_global_daily_limit()
    await update.message.reply_text(
        f"📊 *Stats*\n\n"
        f"Users:`{s['users']}` | Total:`{s['total_scans']}` | Today:`{s['today_scans']}`\n"
        f"Cache:`{s['cache']}` | Monitors:`{s['monitors']}`\n"
        f"Daily limit: `{limit}/day`\n\n"
        f"PW:{'✅' if HAS_PLAYWRIGHT else '❌'} "
        f"Stealth:{'✅' if HAS_STEALTH else '❌'} "
        f"cffi:{'✅' if HAS_CURL_CFFI else '❌'} "
        f"DNS:{'✅' if HAS_DNS else '❌'}\n"
        f"Chromium: `{find_chromium() or 'auto'}`",
        parse_mode="Markdown",
    )

async def cmd_setdaily(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Set daily scan limit for ALL users."""
    if not is_admin(update.effective_user.id): return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("Usage: `/setdaily <number>`\nExample: `/setdaily 50`", parse_mode="Markdown")
        return
    n = int(ctx.args[0])
    n = max(1, min(n, 9999))
    await set_global_daily_limit(n)
    await update.message.reply_text(
        f"✅ Daily limit set to *{n} scans/day* for all users.\n"
        f"_(VIP users have no limit)_",
        parse_mode="Markdown",
    )

async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not ctx.args: await update.message.reply_text("Usage: /ban <uid>"); return
    try: await db_set(int(ctx.args[0]),"is_banned",1); await update.message.reply_text(f"🚫 `{ctx.args[0]}` banned.", parse_mode="Markdown")
    except ValueError: await update.message.reply_text("❌ Invalid ID.")

async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not ctx.args: await update.message.reply_text("Usage: /unban <uid>"); return
    try: await db_set(int(ctx.args[0]),"is_banned",0); await update.message.reply_text(f"✅ `{ctx.args[0]}` unbanned.", parse_mode="Markdown")
    except ValueError: await update.message.reply_text("❌ Invalid ID.")

async def cmd_vip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """VIP = no daily limit."""
    if not is_admin(update.effective_user.id): return
    if not ctx.args: await update.message.reply_text("Usage: /vip <uid>"); return
    try:
        await db_set(int(ctx.args[0]),"is_vip",1)
        await update.message.reply_text(f"⭐ `{ctx.args[0]}` VIP (unlimited scans).", parse_mode="Markdown")
    except ValueError: await update.message.reply_text("❌ Invalid ID.")

async def cmd_unvip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not ctx.args: await update.message.reply_text("Usage: /unvip <uid>"); return
    try:
        await db_set(int(ctx.args[0]),"is_vip",0)
        await update.message.reply_text(f"✅ `{ctx.args[0]}` VIP removed.", parse_mode="Markdown")
    except ValueError: await update.message.reply_text("❌ Invalid ID.")

async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not ctx.args: await update.message.reply_text("Usage: /broadcast <msg>"); return
    msg  = " ".join(ctx.args)
    uids = await db_all_uids()
    sent = fail = 0
    ack  = await update.message.reply_text(f"📢 Sending to {len(uids)}...")
    for uid in uids:
        try: await ctx.bot.send_message(uid,f"📢 *Broadcast:*\n\n{msg}",parse_mode="Markdown"); sent+=1
        except Exception: fail+=1
        await asyncio.sleep(0.05)
    try: await ack.edit_text(f"📢 Done ✅{sent} ❌{fail}")
    except Exception: pass

async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """List all users with ID, name, scan stats."""
    if not is_admin(update.effective_user.id): return
    await _send_user_list(update.message.reply_text, page=0)

async def _send_user_list(reply_fn, page=0):
    """Format and send paginated user list."""
    users = await db_all_users()
    PAGE  = 10
    total = len(users)
    start = page * PAGE
    chunk = users[start:start+PAGE]

    today = date.today().isoformat()
    lines = [f"👥 *User List* — {total} users (page {page+1}/{(total-1)//PAGE+1})\n"]

    for i, u in enumerate(chunk, start+1):
        uid   = u["uid"]
        name  = u.get("first_name") or "?"
        uname = f"@{u['username']}" if u.get("username") else "—"
        scans = u.get("scan_count", 0)
        today_scans = u.get("daily_scans", 0) if u.get("daily_date","") == today else 0
        limit = "∞" if u.get("is_vip") else str(u.get("daily_limit", DAILY_LIMIT))

        badge = ""
        if u.get("is_vip"):    badge = " ⭐VIP"
        if u.get("is_banned"): badge = " 🚫Ban"

        lines.append(
            f"`{i}.` *{name}*{badge} {uname}\n"
            f"   ID: `{uid}` | Scans: `{scans}` | Today: `{today_scans}` | Limit: `{limit}/day`"
        )

    msg = "\n".join(lines)

    # Pagination buttons
    btns = []
    if page > 0:
        btns.append(InlineKeyboardButton("◀️ Prev", callback_data=f"adm:users:{page-1}"))
    if start + PAGE < total:
        btns.append(InlineKeyboardButton("Next ▶️", callback_data=f"adm:users:{page+1}"))
    kb = InlineKeyboardMarkup([btns]) if btns else None

    await reply_fn(msg, parse_mode="Markdown", reply_markup=kb)

async def cmd_clearcache(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    await db_clear_cache(); await update.message.reply_text("🗑 Cache cleared!")

# ── Callbacks ─────────────────────────────────

async def on_admin_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return
    action = q.data.replace("adm:","")
    if action=="stats":      await cmd_stats(update,ctx)
    elif action.startswith("users"):
        page = int(action.split(":")[1]) if ":" in action else 0
        await _send_user_list(q.message.reply_text, page=page)
    elif action=="clearcache": await db_clear_cache(); await q.message.reply_text("🗑 Cleared!")
    elif action=="monitors":
        monitors = await db_get_monitors()
        lines = ["🔔 *Monitors:*\n"] if monitors else ["🔔 None."]
        for m in monitors: lines.append(f"• `{m['domain']}` every {m['interval_h']}h")
        await q.message.reply_text("\n".join(lines),parse_mode="Markdown")
    elif action=="broadcast": await q.message.reply_text("Use `/broadcast msg`",parse_mode="Markdown")
    elif action=="help":
        await q.message.reply_text(
            "🛡 *Admin:*\n`/admin` `/stats`\n"
            "`/setdaily <n>` — daily limit for all\n"
            "`/ban /unban /vip /unvip`\n"
            "`/broadcast /clearcache`",
            parse_mode="Markdown")

async def on_rescan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    raw = q.data.replace("rs:","",1)
    if is_scanning(uid): await q.message.reply_text("⚠️ Scan run နေတယ်。"); return
    ok, remaining = await db_check_daily(uid)
    if not ok:
        limit = await get_global_daily_limit()
        await q.message.reply_text(f"❌ Daily limit ({limit}/day) reached."); return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes",callback_data=f"drs:{raw}"),
        InlineKeyboardButton("❌ No", callback_data="nrs"),
    ]])
    await q.message.reply_text(f"🔄 Rescan `{urlparse(norm(raw)).netloc}`?",parse_mode="Markdown",reply_markup=kb)

async def on_do_rescan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    raw = q.data.replace("drs:","",1)
    try: await q.message.delete()
    except Exception: pass
    if is_scanning(uid): return
    await run_scans(uid,[raw],q.message.reply_text,scanned_by=dname(q.from_user))

async def on_no_rescan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer("Cancelled.")
    try: await q.message.delete()
    except Exception: pass

async def on_export_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    raw = q.data.replace("ex:","",1)
    dom = urlparse(norm(raw)).netloc or raw
    results = [r for r in _last_results.get(uid,[]) if r["domain"]==dom]
    if not results: await q.message.reply_text("❌ No cached result."); return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📄 TXT",  callback_data=f"ef:txt:{raw}"),
        InlineKeyboardButton("📊 CSV",  callback_data=f"ef:csv:{raw}"),
        InlineKeyboardButton("🔧 JSON", callback_data=f"ef:json:{raw}"),
    ]])
    await q.message.reply_text("Format ရွေးပါ:", reply_markup=kb)

async def on_exp_fmt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid   = q.from_user.id
    parts = q.data.split(":",2)
    fmt_  = parts[1] if len(parts)>1 else "txt"
    raw   = parts[2] if len(parts)>2 else ""
    dom   = urlparse(norm(raw)).netloc or raw
    results = [r for r in _last_results.get(uid,[]) if r["domain"]==dom]
    if not results: await q.message.reply_text("❌ No result."); return
    if fmt_=="csv":   content,fn = export_csv(results),  f"{dom}.csv"
    elif fmt_=="json": content,fn = export_json(results), f"{dom}.json"
    else:             content,fn = export_txt(results),   f"{dom}.txt"
    bio = io.BytesIO(content.encode()); bio.name = fn
    await q.message.reply_document(document=bio,filename=fn,caption=f"📊 {dom} — {fmt_.upper()}")

async def on_err(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("PTB: %s", ctx.error, exc_info=ctx.error)

# ══════════════════════════════════════════════
#  COMMAND MENU
# ══════════════════════════════════════════════

USER_CMDS = [
    BotCommand("start",     "🚀 Start"),
    BotCommand("check",     "🔍 Scan — /check stripe.com"),
    BotCommand("fresh",     "🔄 Force rescan"),
    BotCommand("bulk",      "📎 Upload .txt"),
    BotCommand("last",      "📋 Last results"),
    BotCommand("export",    "📊 Export (txt/csv/json)"),
    BotCommand("monitor",   "🔔 /monitor site.com 6"),
    BotCommand("unmonitor", "🔕 Stop monitor"),
    BotCommand("cancel",    "⏹ Cancel scan"),
    BotCommand("help",      "📖 Help"),
]

ADMIN_CMDS = USER_CMDS + [
    BotCommand("admin",      "🛡 Admin panel"),
    BotCommand("stats",      "📊 Stats"),
    BotCommand("users",      "👥 User list with IDs"),
    BotCommand("setdaily",   "📅 /setdaily <n> — daily limit all users"),
    BotCommand("ban",        "🚫 /ban <uid>"),
    BotCommand("unban",      "✅ /unban <uid>"),
    BotCommand("vip",        "⭐ /vip <uid> — unlimited"),
    BotCommand("unvip",      "⬇️ /unvip <uid>"),
    BotCommand("broadcast",  "📢 /broadcast <msg>"),
    BotCommand("clearcache", "🗑 Clear cache"),
]

# ══════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════

def main():
    global _pool
    _pool = BrowserPool(BROWSER_POOL_SZ)

    async def post_init(app):
        await db_init()
        await _pool.start()
        await app.bot.set_my_commands(USER_CMDS, scope=BotCommandScopeAllPrivateChats())
        for aid in ADMIN_IDS:
            try: await app.bot.set_my_commands(ADMIN_CMDS, scope=BotCommandScopeChat(chat_id=aid))
            except Exception as e: log.warning("Admin cmd %d: %s",aid,e)
        for m in await db_get_monitors():
            key = f"{m['uid']}:{m['domain']}"
            if key not in _monitors:
                task = asyncio.create_task(monitor_loop(app,m["uid"],m["domain"],m["interval_h"]))
                _monitors[key] = {"task":task}
        log.info("Bot v10 ready ✓ | Chromium: %s", find_chromium() or "auto")

    async def post_shutdown(app):
        await _pool.stop()

    log.info("="*50)
    log.info("Site Checker Bot v10 ULTIMATE")
    log.info("GW:%d PLT:%d | PW:%s Stealth:%s cffi:%s DNS:%s",
             len(GW),len(PLT),
             "✅" if HAS_PLAYWRIGHT else "❌",
             "✅" if HAS_STEALTH    else "❌",
             "✅" if HAS_CURL_CFFI  else "❌",
             "✅" if HAS_DNS        else "❌")
    log.info("="*50)

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .concurrent_updates(True)
        .build()
    )

    for cmd,handler in [
        ("start",cmd_start),("help",cmd_help),("check",cmd_check),
        ("fresh",cmd_fresh),("cancel",cmd_cancel),("last",cmd_last),
        ("export",cmd_export),("bulk",cmd_bulk),
        ("monitor",cmd_monitor),("unmonitor",cmd_unmonitor),
        ("admin",cmd_admin),("stats",cmd_stats),("users",cmd_users),
        ("setdaily",cmd_setdaily),
        ("ban",cmd_ban),("unban",cmd_unban),
        ("vip",cmd_vip),("unvip",cmd_unvip),
        ("broadcast",cmd_broadcast),("clearcache",cmd_clearcache),
    ]:
        app.add_handler(CommandHandler(cmd,handler))

    app.add_handler(CallbackQueryHandler(on_admin_cb,  pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(on_rescan,    pattern=r"^rs:"))
    app.add_handler(CallbackQueryHandler(on_do_rescan, pattern=r"^drs:"))
    app.add_handler(CallbackQueryHandler(on_no_rescan, pattern=r"^nrs$"))
    app.add_handler(CallbackQueryHandler(on_export_cb, pattern=r"^ex:"))
    app.add_handler(CallbackQueryHandler(on_exp_fmt,   pattern=r"^ef:"))
    app.add_handler(MessageHandler(filters.Document.FileExtension("txt"), on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_msg))
    app.add_error_handler(on_err)

    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
