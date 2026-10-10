import urllib.request
url="https://adworld.xctf.org.cn/file/ad/tenant/default/question_attachment/834/621/b5684ebf8bf4f57da90980b956ba55fb.zip?attname=5712ec10df6b47dca031b559c61e8b13.zip"
try:
    r=urllib.request.urlopen(url,timeout=50)
    data=r.read()
    open("att.zip","wb").write(data)
    print("ok",len(data))
except Exception as e:
    print("err",e)
