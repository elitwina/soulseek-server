# אפשרויות אירוח לשרת Flask עם WebSocket

## אפשרויות מומלצות (ללא restart יומי)

### 1. **Railway.app** ⭐ מומלץ ביותר
- **מחיר**: $5/חודש (או $0 עם $5 credit חינמי כל חודש)
- **יתרונות**:
  - אין restart יומי (always-on)
  - תמיכה מלאה ב-WebSocket
  - פריסה קלה מ-GitHub
  - סביבה דומה ל-Heroku
- **הגבלות**: 
  - $5 credit חינמי כל חודש (מספיק לרוב האפליקציות)
  - אחרי זה $5/חודש
- **קישור**: https://railway.app

### 2. **Fly.io** ⭐ מומלץ
- **מחיר**: חינמי (עם הגבלות) או $1.94/חודש
- **יתרונות**:
  - אין restart יומי
  - תמיכה מעולה ב-WebSocket
  - פריסה מהירה
  - 3 VMs חינמיים (256MB RAM כל אחד)
- **הגבלות**:
  - 3GB outbound data transfer חינמי
  - אחרי זה תשלום לפי שימוש
- **קישור**: https://fly.io

### 3. **Render**
- **מחיר**: חינמי (עם הגבלות) או $7/חודש
- **יתרונות**:
  - אין restart יומי בתוכנית בתשלום
  - תמיכה ב-WebSocket
  - פריסה אוטומטית מ-GitHub
- **הגבלות**:
  - תוכנית חינמית: sleep אחרי 15 דקות של חוסר פעילות
  - תוכנית בתשלום ($7/חודש): always-on
- **קישור**: https://render.com

### 4. **DigitalOcean App Platform**
- **מחיר**: $5/חודש
- **יתרונות**:
  - אין restart יומי
  - תמיכה ב-WebSocket
  - יציב ואמין
- **קישור**: https://www.digitalocean.com/products/app-platform

### 5. **VPS זול** (Vultr/DigitalOcean/Linode)
- **מחיר**: $2.50-6/חודש
- **יתרונות**:
  - שליטה מלאה
  - אין הגבלות
  - תמיכה מלאה ב-WebSocket
- **חסרונות**:
  - צריך לנהל בעצמך (systemd, nginx וכו')
  - יותר עבודה בהגדרה
- **קישורים**:
  - Vultr: https://www.vultr.com ($2.50/חודש)
  - DigitalOcean: https://www.digitalocean.com ($4/חודש)
  - Linode: https://www.linode.com ($5/חודש)

## Heroku - אפשר למנוע restart?

### אפשרות 1: Kaffeine (חינמי)
- שירות חינמי ששולח requests כל 30 דקות כדי למנוע sleep
- **לא מונע restart יומי** - רק מונע sleep
- **קישור**: https://kaffeine.herokuapp.com/

### אפשרות 2: Worker Dyno (בתשלום)
- Worker dynos לא מתאפסים כל 24 שעות
- **מחיר**: $7/חודש
- אבל צריך להוסיף web dyno גם ($7/חודש) = $14/חודש

**מסקנה**: Heroku לא מומלץ - יקר מדי או עדיין מתאפס

## המלצה סופית

**לשימוש חינמי/זול**: **Railway.app** או **Fly.io**
- Railway: $5 credit חינמי כל חודש (בדרך כלל מספיק)
- Fly.io: 3 VMs חינמיים עם 3GB transfer

**לשימוש יציב יותר**: **VPS מ-Vultr** ($2.50/חודש)
- הכי זול
- שליטה מלאה
- אבל צריך לנהל בעצמך

## הוראות פריסה

### Railway.app
1. הירשם ב-https://railway.app
2. New Project → Deploy from GitHub
3. בחר את ה-repo שלך
4. הוסף environment variables:
   - `USERNAME=DjLic`
   - `PASSWORD=DjLic`
5. הוסף `Procfile`:
   ```
   web: cd server && python server.py
   ```

### Fly.io
1. התקן flyctl: `curl -L https://fly.io/install.sh | sh`
2. `fly launch` בתיקיית הפרויקט
3. ערוך `fly.toml`:
   ```toml
   [build]
     builder = "paketobuildpacks/builder:base"
   
   [env]
     USERNAME = "DjLic"
     PASSWORD = "DjLic"
   
   [[services]]
     internal_port = 8001
     protocol = "tcp"
   ```
4. `fly deploy`

### Render
1. הירשם ב-https://render.com
2. New → Web Service
3. בחר את ה-repo שלך
4. הגדרות:
   - Build Command: `pip install -r server/requirements.txt`
   - Start Command: `cd server && python server.py`
   - Environment Variables: `USERNAME`, `PASSWORD`
5. בחר תוכנית בתשלום ($7/חודש) ל-always-on

