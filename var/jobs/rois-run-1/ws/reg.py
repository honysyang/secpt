import urllib.request, urllib.parse, http.cookiejar, sys

IDP = "http://oj-10-30-15-1-56469.adworld.xctf.org.cn"
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

def post(url, data):
    d = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=d)
    try:
        r = op.open(req, timeout=25)
        return r.getcode(), r.geturl(), r.read().decode('utf-8', 'ignore')
    except urllib.error.HTTPError as e:
        return e.code, url, e.read().decode('utf-8','ignore')

uname = sys.argv[1] if len(sys.argv)>1 else "attlab002"
email = uname + "@x.com"
code, u, body = post(IDP+"/register", {
    "username": uname, "email": email, "password": "pass123456",
    "confirmPassword": "pass123456", "type": "true", "displayName": "a", "department": "d"
})
print("REG", code, u)
print(body[:800])
