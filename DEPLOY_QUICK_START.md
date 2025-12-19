# פריסה מהירה ל-Railway - רק שרת

## שלב 1: יצירת Git Repository

```bash
cd /Users/djlic-14/Desktop/programming/soulseek
git init
git add .
git commit -m "Initial commit - Soulseek server"
```

## שלב 2: העלאה ל-GitHub

1. צור repository חדש ב-GitHub: https://github.com/new
   - שם: `soulseek-server` (או שם אחר)
   - **אל תסמן** "Initialize with README"
   - לחץ "Create repository"

2. העלה את הקוד:
```bash
git remote add origin https://github.com/YOUR_USERNAME/soulseek-server.git
git branch -M main
git push -u origin main
```

(החלף `YOUR_USERNAME` בשם המשתמש שלך)

## שלב 3: פריסה ב-Railway

1. **היכנס ל-Railway:**
   - https://railway.app
   - התחבר עם GitHub

2. **צור פרויקט:**
   - לחץ "New Project"
   - בחר "Deploy from GitHub repo"
   - בחר את ה-repo שיצרת

3. **הגדר Environment Variables:**
   - לחץ על השירות → Variables
   - הוסף:
     - `USERNAME` = `DjLic`
     - `PASSWORD` = `DjLic`

4. **קבל את ה-URL:**
   - Railway ייצור URL אוטומטי
   - העתק את ה-URL (למשל: `soulseek-server.up.railway.app`)

## שלב 4: עדכון הקליינט המקומי

עדכן את `client/client.py` עם כתובת השרת החדשה:

```python
SERVER = "https://YOUR_APP_NAME.up.railway.app"
```

## הערות

- רק השרת יועלה (תיקיית `client/` לא תועלה)
- השרת לא יתאפס כל 24 שעות
- Railway נותן $5 credit חינמי כל חודש



