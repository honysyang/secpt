import urllib.request, urllib.parse, http.cookiejar, re

IDP = "http://oj-10-30-15-1-58595.adworld.xctf.org.cn"
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
op.addheaders = [('User-Agent','Mozilla/5.0')]

op.open(IDP+"/login", data=urllib.parse.urlencode({"username":"admin","password":"RCTF2025"}).encode(), timeout=30)
html = op.open(IDP+"/saml/idp/Flag", timeout=30).read().decode('utf-8','replace')

# print the form portion
i = html.find('<form')
print(html[i:i+4000])
# extract action
m = re.search(r'action="([^"]*)"', html)
print("\nACTION=", m.group(1) if m else None)
open("/home/kali/secpt/var/jobs/ab-single-3/ws/sso_form.html","w").write(html)
