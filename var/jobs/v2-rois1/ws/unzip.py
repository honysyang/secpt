import zipfile
z=zipfile.ZipFile('attach.zip')
for n in z.namelist():
    print(n)
z.extractall('src')
