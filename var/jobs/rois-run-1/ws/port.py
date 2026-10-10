import urllib.request, urllib.parse, http.cookiejar, sys, re

IDP = "http://oj-10-30-15-1-56469.adworld.xctf.org.cn"
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

def post(url, data):
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

uname = "attlab003"
email = uname + "@x.com"
print(post(IDP+"/register", {
    "username": uname, "email": email, "password": "pass123456",
    "confirmPassword": "pass123456", "type": "true", "displayName": "a", "department": "d"
})[1])

# logout then login to refresh session type from DB
get(IDP+"/logout")
post(IDP+"/login", {"username": uname, "password": "pass123456"})
code, u, body = get(IDP+"/portal")
print("PORTAL", code, u)
# print service links
for m in re.findall(r'href="([^"]*)"', body):
    print("LINK", m)
print("---- snippet ----")
print(body[body.find('service')-200: body.find('service')+2000] if 'service' in body else body[:1500])
