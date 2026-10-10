import urllib.request, urllib.parse, http.cookiejar, re, sys

IDP = "http://oj-10-30-15-1-56469.adworld.xctf.org.cn"
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
        return resp.geturl(), resp.read().decode('utf-8','replace')
    except urllib.error.HTTPError as e:
        return e.geturl(), e.read().decode('utf-8','replace')

# Step 1: hit SP saml/login -> should redirect to IDP with SAMLRequest
url, body = req(SP + "/saml/login")
print("== after SP /saml/login ==", url)
print(body[:300])
print("cookies:", [c.name for c in cj])

# The opener follows redirects automatically; url should now be IDP site.
# If we landed on IDP login page, extract samlRequest/relayState hidden fields
if 'login' in url or '/login' in body:
    m = re.search(r'name="samlRequest"\s+value="([^"]*)"', body)
    samlRequest = m.group(1) if m else ''
    m2 = re.search(r'name="relayState"\s+value="([^"]*)"', body)
    relayState = m2.group(1) if m2 else ''
    print("samlRequest len", len(samlRequest), "relayState", relayState)
    login_url = url
    url, body = req(login_url, data={
        'username':'admin',
        'password':'RCTF2025',
        'samlRequest':samlRequest,
        'relayState':relayState,
    })
    print("== after login POST ==", url)
    print(body[:500])

print("FINAL URL:", url)
print(body[:2000])
