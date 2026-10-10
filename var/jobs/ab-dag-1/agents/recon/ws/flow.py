import urllib.request, urllib.parse, http.cookiejar, re, base64, zlib, sys

IDP='http://oj-10-30-15-1-58595.adworld.xctf.org.cn'
SP='http://oj-10-30-15-1-49263.adworld.xctf.org.cn'

cj=http.cookiejar.CookieJar()
op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
op.addheaders=[('User-Agent','Mozilla/5.0')]

def get(u):
    r=op.open(u,timeout=20); return r.status, r.read().decode('utf-8','replace'), r.url
def post(u,data):
    d=urllib.parse.urlencode(data).encode()
    r=op.open(urllib.request.Request(u,data=d),timeout=20); return r.status, r.read().decode('utf-8','replace'), r.url

st,b,u=get(IDP+'/register')
st,b,u=post(IDP+'/register', {'type':'1','username':'testuser1','email':'t1@x.com','password':'pass1234','confirmPassword':'pass1234','displayName':'T1','department':'IT'})
print('register status',st,'url',u)
sys.stdout.write(b[:2500])
