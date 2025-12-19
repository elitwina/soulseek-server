# Soulseek Download Service

פרויקט להורדת שירים מ-Soulseek דרך API.

## מבנה הפרויקט

```
soulseek/
├── server/          # תיקיית השרת
│   ├── server.py    # שרת Flask עם WebSocket
│   ├── slsk_service.py  # לוגיקת Soulseek
│   ├── downloads/  # תיקיית הורדות (נמחקת אוטומטית אחרי שליחה)
│   ├── requirements.txt
│   └── run_server.sh
│
├── client/          # תיקיית הקליינט
│   ├── client.py    # קליינט Python
│   ├── requirements.txt
│   └── run_client.sh
│
└── requirements.txt # תלויות כלליות (legacy)
```

## התקנה

1. צור virtual environment:
```bash
python -m venv .venv
source .venv/bin/activate  # או .venv\Scripts\activate ב-Windows
```

2. התקן תלויות:
```bash
# לשרת
pip install -r server/requirements.txt

# לקליינט
pip install -r client/requirements.txt
```

## הרצה

### שרת:
```bash
cd server
./run_server.sh
```

או:
```bash
cd server
source ../.venv/bin/activate
python server.py
```

### קליינט:
```bash
cd client
./run_client.sh "שם השיר"
```

או:
```bash
cd client
source ../.venv/bin/activate
python client.py "שם השיר"
```

## הערות

- השרת רץ על פורט 8001
- קבצים נמחקים מהשרת אוטומטית אחרי שהקליינט סיים להוריד
- הקליינט שומר את הקבצים בתיקייה שבה הוא רץ



