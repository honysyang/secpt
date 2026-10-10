import urllib.request, urllib.parse, http.cookiejar, re, sys

IDP = "http://oj-10-30-15-1-58595.adworld.xctf.org.cn"
SP  = "http://oj-10-30-15-1-49263.adworld.xctf.org.cn"

cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
op.addheaders = [('User-Agent','Mozilla/5.0')]

def open_(u):
    try:
        r = op.open(u, timeout=20)
        return r.geturl(), r.read().decode('utf8','replace')
    except urllib.error.HTTPError as e:
        return e.geturl(), e.read().decode('utf8','replace')

def post(u, data):
    d = urllib.parse.urlencode(data).encode()
    try:
        r = op.open(urllib.request.Request(u, data=d), timeout=20)
        return r.geturl(), r.read().decode('utf8','replace')
    except urllib.error.HTTPError as e:
        return e.geturl(), e.read().decode('utf8','replace')

# login
post(IDP+"/login", {"username":"admin","password":"RCTF2025"})

# SP endpoints probe
for path in ["/", "/saml/login", "/saml/metadata", "/login"]:
    u,s = open_(SP+path)
    print("SP", path, "->", u, "len", len(s))
    if "SAMLRequest" in s or "form" in s.lower():
        print(s[:600])

# idp initiated
for path in ["/saml/sso/Flag", "/saml/init/Flag", "/saml/sso?serviceId=Flag"]:
    u,s = open_(IDP+path)
    print("IDP", path, "->", u, "len", len(s), "SAMLResponse" in s)
    if "SAMLResponse" in s:
        m = re.search(r'name="SAMLResponse"\s+value="([^"]+)"', s)
        print("RESP found:", bool(m))
        if m:
            open("/home/kali/secpt/var/jobs/ab-single-1/ws/real_resp.b64","w").write(m.group(1))
        acs = re.search(r'action="([^"]+)"', s)
        print("action:", acs.group(1) if acs else None)
