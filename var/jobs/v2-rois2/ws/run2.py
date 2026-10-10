import urllib.request, urllib.parse, http.cookiejar, re

SP  = "http://oj-10-30-15-1-44153.adworld.xctf.org.cn"

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
opener.addheaders = [('User-Agent','Mozilla/5.0')]

def req(url, data=None, method=None):
    if data is not None and isinstance(data, dict):
        data = urllib.parse.urlencode(data).encode()
    r = urllib.request.Request(url, data=data, method=method or ('POST' if data is not None else 'GET'))
    try:
        resp = opener.open(r, timeout=30)
        return resp.geturl(), resp.read().decode('utf-8','replace'), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.geturl(), e.read().decode('utf-8','replace'), dict(e.headers)

url, body, hdr = req(SP + "/saml/login")
print("== after SP /saml/login ==", url)
print("headers:", hdr)
print(body[:1500])
print("cookies:", [(c.name,c.value) for c in cj])
