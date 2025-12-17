# הוראות פריסה ל-Railway

## שלב 1: יצירת Git Repository

1. **אתחל git repository:**
```bash
cd /Users/djlic-14/Desktop/programming/soulseek
git init
git add .
git commit -m "Initial commit - Soulseek server only"
```

**הערה:** תיקיית `client/` לא תועלה (מוגדרת ב-.gitignore)

2. **צור repository ב-GitHub:**
   - לך ל-https://github.com/new
   - צור repository חדש (למשל: `soulseek-server`)
   - **אל תסמן** "Initialize with README"
   - לחץ "Create repository"

3. **העלה את הקוד ל-GitHub:**
```bash
git remote add origin https://github.com/YOUR_USERNAME/soulseek-server.git
git branch -M main
git push -u origin main
```

(החלף `YOUR_USERNAME` בשם המשתמש שלך ב-GitHub)

## שלב 2: פריסה ב-Railway

1. **היכנס ל-Railway:**
   - לך ל-https://railway.app
   - התחבר עם GitHub

2. **צור פרויקט חדש:**
   - לחץ "New Project"
   - בחר "Deploy from GitHub repo"
   - בחר את ה-repo שיצרת (`soulseek-server`)

3. **הגדר את השרת:**
   - Railway יזהה אוטומטית את ה-`Procfile`
   - אם לא, לחץ על השירות → Settings → Deploy
   - וודא ש-Start Command הוא: `cd server && python server.py`

4. **הוסף Environment Variables:**
   - לחץ על השירות → Variables
   - הוסף:
     - `USERNAME` = `DjLic`
     - `PASSWORD` = `DjLic`
     - `PORT` = `8001` (אופציונלי - Railway יקבע אוטומטית)

5. **הגדר את הפורט:**
   - לחץ על השירות → Settings → Networking
   - הוסף Port: `8001` (או השאר את הפורט ש-Railway קבע)
   - Railway ייצור URL אוטומטי (למשל: `soulseek-server.up.railway.app`)

## שלב 3: עדכון הקליינט

עדכן את `client/client.py` עם כתובת השרת החדשה:

```python
SERVER = "https://YOUR_APP_NAME.up.railway.app"
```

או אם Railway נתן לך domain אחר, השתמש בו.

## שלב 4: בדיקה

1. **בדוק שהשרת רץ:**
   - לך ל-`https://YOUR_APP_NAME.up.railway.app` בדפדפן
   - אתה אמור לראות שגיאה 404 (זה תקין - אין endpoint ב-root)

2. **בדוק עם הקליינט:**
```bash
cd client
./run_client.sh "jamback - Positive Extended Mix"
```

## הערות חשובות

- Railway נותן $5 credit חינמי כל חודש
- השרת לא יתאפס כל 24 שעות (always-on)
- הקבצים יימחקו אוטומטית אחרי שליחה לקליינט
- אם אתה צריך לשנות משהו, פשוט `git push` והשרת יתעדכן אוטומטית

## פתרון בעיות

**אם השרת לא עולה:**
- בדוק את ה-Logs ב-Railway
- וודא שה-requirements.txt נכון
- וודא שה-`Procfile` נכון

**אם WebSocket לא עובד:**
- וודא ש-Railway תומך ב-WebSocket (תמיכה מלאה)
- בדוק שה-PORT נכון

