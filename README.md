# PS Gift Card Stock Monitor 🎮

בודק ברציפות אם [כרטיס המתנה של PlayStation Store ב-₹2000](https://www.amazon.in/dp/B093QF35KZ) חזר למלאי באמאזון הודו, ושולח הודעת טלגרם ברגע שהוא זמין.

- 🟢 הודעה ברגע שהמוצר חוזר למלאי (ותזכורת כל דקה כל עוד הוא במלאי)
- 🔴 הודעה אם הוא אוזל שוב
- 🛡️ אם אמאזון חוסמת (captcha / 503), הסקריפט מאט אוטומטית ואז חוזר לקצב הרגיל

## 1. הקמת בוט טלגרם

1. בטלגרם פתח את [@BotFather](https://t.me/BotFather), שלח `/newbot` ושמור את ה-**token**.
2. שלח לבוט החדש הודעה כלשהי (למשל `hi`).
3. פתח בדפדפן `https://api.telegram.org/bot<TOKEN>/getUpdates` והעתק את `chat.id` — זה ה-**chat id**.

## 2. הרצה על המחשב / שרת (מומלץ, בדיקה כל שנייה)

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN="123456789:AA..."
export TELEGRAM_CHAT_ID="123456789"
python monitor.py
```

כשהסקריפט עולה תגיע הודעת "👀 Stock monitor started" — ככה יודעים שהטלגרם מחובר.

להרצה ברקע על שרת Linux: `nohup python monitor.py > monitor.log 2>&1 &`

## 3. הרצה על GitHub Actions (בלי מחשב דלוק)

1. ב-**Settings → Secrets and variables → Actions** הוסף את הסודות `TELEGRAM_BOT_TOKEN` ו-`TELEGRAM_CHAT_ID`.
2. ב-**Actions → Stock monitor → Run workflow** להפעלה ידנית.

כל ריצה בודקת בלולאה ~6 שעות, וה-cron מפעיל אותה מחדש כך שהמעקב רציף.
**שים לב:** מעקב רציף צורך ~43,000 דקות Actions בחודש — זה חינמי רק ב-repo **ציבורי**.
ב-repo פרטי הריצות המתוזמנות מדלגות אוטומטית (כדי לא לשרוף את המכסה); כדי שירוץ לבד צריך להפוך את ה-repo לציבורי (הטוקנים שמורים כ-secrets ולא נחשפים).

## הגדרות (משתני סביבה)

| משתנה | ברירת מחדל | תיאור |
|---|---|---|
| `ASIN` | `B093QF35KZ` | מזהה המוצר באמאזון (אפשר לעקוב אחרי מוצר אחר) |
| `CHECK_INTERVAL` | `1` | שניות בין בדיקות (ב-Actions: `2`) |
| `REMIND_EVERY` | `60` | כל כמה שניות לשלוח תזכורת כשבמלאי |
| `MAX_RUNTIME` | `0` | זמן ריצה מקסימלי בשניות (`0` = לנצח) |

## ⚠️ לגבי בדיקה כל שנייה

אמאזון מזהה בקשות תכופות ומחזירה captcha. הסקריפט מתמודד עם זה ב-backoff אוטומטי, אבל אם רואים הרבה `Blocked by Amazon` בלוג — כדאי להעלות את `CHECK_INTERVAL` ל-5–10 שניות. בפועל זה מהיר יותר מבדיקה כל שנייה שנחסמת כל הזמן.

## בדיקות

```bash
pip install pytest && pytest
```
