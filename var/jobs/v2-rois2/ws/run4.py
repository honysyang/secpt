import urllib.request, urllib.parse, http.cookiejar, re

SP  = "http://oj-10-30-15-1-44153.adworld.xctf.org.cn"
IDP = "http://oj-10-30-15-1-56469.adworld.xctf.org.cn"

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
opener.addheaders = [('User-Agent','Mozilla/5.0')]

def req(url, data=None, method=None, follow=True):
    if data is not None and isinstance(data, dict):
        data = urllib.parse.urlencode(data).encode()
    r = urllib.request.Request(url, data=data, method=method or ('POST' if data is not None else 'GET'))
    try:
        resp = opener.open(r, timeout=30)
        return resp.geturl(), resp.read().decode('utf-8','replace'), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.geturl(), e.read().decode('utf-8','replace'), dict(e.headers)

# Step 1: SP /saml/login -> get redirect location (127.0.0.1 -> replace)
url, body, hdr = req(SP + "/saml/login")
loc = hdr.get('Location')
print("SP redirect:", loc)
loc = loc.replace("http://127.0.0.1:80", IDP)
print("fixed:", loc)

# Step 2: hit IdP saml/sso
u2, b2, h2 = req(loc)
print("== IdP saml/sso ==", u2, h2.get('Location'))
print(b2[:3000])
print("cookies:", [(c.name,c.value) for c in cj])
