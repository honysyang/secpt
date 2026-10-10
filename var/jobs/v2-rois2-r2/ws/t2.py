import urllib.request, urllib.parse, http.cookiejar, re, sys

IDP = "http://oj-10-30-15-1-56469.adworld.xctf.org.cn"
SP  = "http://oj-10-30-15-1-44153.adworld.xctf.org.cn"

cj = http.cookiejar.CookieJar()
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), NoRedirect())

def req(url, data=None, hdr=None):
    h = {"User-Agent":"curl/8"}
    if hdr: h.update(hdr)
    if data is not None:
        data = urllib.parse.urlencode(data).encode()
        h["Content-Type"]="application/x-www-form-urlencoded"
    r = urllib.request.Request(url, data=data, headers=h)
    try:
        resp = op.open(r, timeout=25)
        return resp.status, dict(resp.headers), resp.read().decode('utf-8','replace')
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode('utf-8','replace')

# 1. SP /saml/login -> redirect to IDP /login?SAMLRequest=...&RelayState=...
st, h, b = req(SP+"/saml/login")
print("SP /saml/login:", st, h.get("Location"))
loc = h.get("Location")
assert loc, b[:300]

# 2. fetch IDP login page to get samlRequest hidden fields
st, h, b = req(loc)
print("IDP login page:", st)
samlReq = re.search(r'name="samlRequest"\s+value="([^"]*)"', b)
relay   = re.search(r'name="relayState"\s+value="([^"]*)"', b)
print("samlRequest field:", (samlReq.group(1)[:80] if samlReq else None))
print("relayState field:", (relay.group(1) if relay else None))
# fall back to query params
q = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
SAMLRequest = q.get("SAMLRequest",[""])[0]
RelayState  = q.get("RelayState",[""])[0]
print("query SAMLRequest len:", len(SAMLRequest), "RelayState:", RelayState)

# 3. POST login with samlRequest -> redirect to /saml/sso?...
st, h, b = req(IDP+"/login", {"username":"admin","password":"RCTF2025",
                              "samlRequest":SAMLRequest,"relayState":RelayState})
print("login resp:", st, h.get("Location"))
sso = h.get("Location")

# 4. GET /saml/sso -> renders saml_post with SAMLResponse
st, h, b = req(IDP+sso if not sso.startswith("http") else sso)
print("sso page:", st)
print(b[:1500])
