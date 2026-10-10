import urllib.request, urllib.parse, http.cookiejar, sys

IDP="http://oj-10-30-15-1-58595.adworld.xctf.org.cn"
SP="http://oj-10-30-15-1-49263.adworld.xctf.org.cn"

cj=http.cookiejar.CookieJar()
# manual redirect handling to inspect
op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

def get(u, follow=True):
    req=urllib.request.Request(u)
    try:
        r=op.open(req, timeout=20)
        return r.status, r.read().decode('utf-8','replace'), r.geturl(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8','replace'), u, dict(e.headers)

def post(u, data):
    d=urllib.parse.urlencode(data).encode()
    req=urllib.request.Request(u, data=d, headers={'Content-Type':'application/x-www-form-urlencoded'})
    try:
        r=op.open(req, timeout=20)
        return r.status, r.read().decode('utf-8','replace'), r.geturl(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8','replace'), u, dict(e.headers)

# Step 1: SP login -> 302 to IdP SSO
s,b,u,h=get(SP+"/saml/login")
print("SP login:", s, u)
print("location:", h.get('Location'))
idp_sso = h.get('Location')
