import os
for root, dirs, files in os.walk('src/auth'):
    for f in files:
        p = os.path.join(root, f)
        print(os.path.getsize(p), p)
