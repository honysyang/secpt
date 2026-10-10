import urllib.request, urllib.parse, http.cookiejar, sys, re, base64

IDP = "http://oj-10-30-15-1-56469.adworld.xctf.org.cn"
SP  = "http://oj-10-30-15-1-44153.adworld.xctf.org.cn"
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

def post(url, data, raw=False):
    d = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=d)
    try:
        r = op.open(req, timeout=25)
        return r.getcode(), r.geturl(), r.read().decode('utf-8','ignore')
    except urllib.error.HTTPError as e:
        return e.code, url, e.read().decode('utf-8','ignore')

def get(url):
    try:
        r = op.open(url, timeout=25)
        return r.getcode(), r.geturl(), r.read().decode('utf-8','ignore')
    except urllib.error.HTTPError as e:
        return e.code, url, e.read().decode('utf-8','ignore')

uname = "attlab004"
email = uname + "@x.com"
post(IDP+"/register", {"username":uname,"email":email,"password":"pass123456",
    "confirmPassword":"pass123456","type":"true","displayName":"a","department":"d"})
get(IDP+"/logout")
post(IDP+"/login", {"username":uname,"password":"pass123456"})

code, u, body = get(IDP+"/saml/idp/Flag")
print("SSO code", code)
# extract acsUrl and SAMLResponse from the auto-post form
acs = re.search(r'name="action"\s+value="([^"]+)"', body) or re.search(r'action="([^"]+)"', body)
print("ACS action:", acs.group(1) if acs else "NOT FOUND")
saml = re.search(r'name="SAMLResponse"\s+value="([^"]+)"', body)
print("has SAMLResponse:", bool(saml))
if saml:
    xml = base64.b64decode(urllib.parse.unquote(saml.group(1))).decode('utf-8','ignore')
    open("/home/kali/secpt/var/jobs/rois-run-1/ws/resp.xml","w").write(xml)
    print(xml[:2000])
