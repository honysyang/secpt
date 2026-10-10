import urllib.request, urllib.parse, http.cookiejar, re, base64

IDP = "http://oj-10-30-15-1-58595.adworld.xctf.org.cn"
SP  = "http://oj-10-30-15-1-49263.adworld.xctf.org.cn"

cj = http.cookiejar.CookieJar()

class NoRedir(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None

def opener(follow):
    if follow:
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), NoRedir())

def openf(u, data=None, follow=True):
    o = opener(follow)
    o.addheaders = [('User-Agent','Mozilla/5.0')]
    try:
        r = o.open(u, data=data, timeout=25)
        return r.status, dict(r.headers), r.read().decode('utf8','replace')
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode('utf8','replace')

openf(IDP+"/login")
st,h,s = openf(IDP+"/login", urllib.parse.urlencode({"username":"admin","password":"RCTF2025"}).encode())
print("login post", st, "portal?", "Portal" in s)

st,h,s = openf(SP+"/saml/login", follow=False)
loc = h.get("Location")
print("SP /saml/login ->", st, (loc or "")[:80])

sso = loc.replace("http://127.0.0.1:80", IDP).replace("http://127.0.0.1", IDP)
st2,h2,s2 = openf(sso, follow=False)
print("SSO status", st2, "loc", h2.get("Location"))
open("/home/kali/secpt/var/jobs/ab-single-1/ws/sso_out.html","w").write(s2)
m = re.search(r'name="SAMLResponse"\s+value="([^"]+)"', s2)
ma = re.search(r'action="([^"]+)"', s2)
print("SAMLResponse:", bool(m), "action:", ma.group(1) if ma else None)
if m:
    open("/home/kali/secpt/var/jobs/ab-single-1/ws/real_resp.b64","w").write(m.group(1))
    print(base64.b64decode(m.group(1)).decode('utf8','replace'))
