# מילון הפקודות של Sentinal Eye

המערכת רצה על Jetson Orin Nano Super עם JetPack 7.2.1 ושתי מצלמות CSI מסוג IMX219. כתובת ה־Jetson היא `192.168.0.29`, שם המשתמש `admin` ותיקיית ההרצה `~/sentinal-eye`.

`run.sh` מפעיל כברירת מחדל את `buffalo_m` על CUDA, עם שתי המצלמות והגלריה `sentinal-people`. אפשר לעצור כל הרצה עם `Ctrl+C`.

## לוח בקרה מרכזי על ה־Jetson

סגור הרצה פעילה של המצלמות והפעל ב־Mac את `Install on Jetson.command`. הזן את כתובת ה־IPv4 הנוכחית של ה־Jetson. המתקין משתמש בחשבון `admin` ב־SSH וב־`sudo` כדי להתקין את `sentinal-eye.service`, שעולה עם ה־Jetson. הגלריה והמודלים ממשיכים להישמר בתיקיית `~/sentinal-eye`.

בסיום, ה־Terminal מציג את כתובת ה־HTTPS ואת הנתיבים לקבצים `panel-password.txt`, `SentinalEye.mobileconfig` ו־`SentinalEyeCA.cer`. באייפד העבר והתקן את `SentinalEye.mobileconfig`, ואז אפשר אמון בתעודה תחת **Settings → General → About → Certificate Trust Settings**. במחשב אחר ייבא את `SentinalEyeCA.cer` כ־CA מהימן לפי הוראות מערכת ההפעלה. [הוראות Apple להפעלת אמון בתעודה באייפד](https://support.apple.com/en-us/102390)

פתח בדפדפן `https://<Jetson IP>:8750/` ממכשיר באותה רשת והיכנס בשם `admin` עם הסיסמה מתוך `panel-password.txt`. הסיסמה הזו שייכת ללוח הבקרה. **Start** מפעיל את המצלמות והזיהוי. **Save and upload** שומר תמונות ופרטים בגלריה על ה־Jetson. אפשר לסגור את ה־Mac אחרי ההתקנה. מומלץ לשמור כתובת קבועה ל־Jetson ברשת. אם הכתובת משתנה, הרץ שוב את המתקין עם הכתובת החדשה והתקן את התעודה החדשה בכל מכשיר.

## הפעלה יומיומית

### מה־Mac עם פלט זיהוי בטרמינל

```bash
ssh -x -t admin@192.168.0.29 'bash ~/sentinal-eye/run.sh'
```

הפלט מציג את השמות כשהזיהוי משתנה וקצב זיהוי לכל מצלמה כל חמש שניות. פנים ללא התאמה מסומנות `Unknown`.

### מה־Mac עם וידאו ב־Chrome

עצור הרצה קודמת שתופסת את המצלמות לפני פתיחת השידור.

```bash
ssh -x -t -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:8765:127.0.0.1:8765 admin@192.168.0.29 \
  'bash ~/sentinal-eye/run.sh --web'
```

כשתופיע `Web preview listening`, השאר את הטרמינל פתוח ופתח ב־Mac את [תצוגת המצלמות](http://127.0.0.1:8765/). הדפדפן מציג את שתי המצלמות, המסגרות, השמות והמודל הפעיל. תצוגת הדפדפן מוגבלת לעד 10 פריימים בשנייה. [תיעוד העברת פורטים ב־OpenSSH](https://man.openbsd.org/ssh.1)

### בטרמינל של ה־Jetson

```bash
# פלט בטרמינל
bash ~/sentinal-eye/run.sh

# וידאו על המסך המחובר ל־Jetson
bash ~/sentinal-eye/run.sh --preview
```

יש להריץ מצב אחד בכל פעם. למצב `--preview` היכנס קודם לשולחן העבודה של Ubuntu.

## העתקת עדכונים מה־Mac ל־Jetson

פתח Terminal מקומי ב־Mac. `uname -s` מחזיר `Darwin` ב־Mac ו־`Linux` ב־Jetson.

```bash
# כניסה רגילה לטרמינל של ה־Jetson
ssh -x admin@192.168.0.29
```

אפשר לחזור מהטרמינל של ה־Jetson ל־Mac עם `exit`. הפקודות הבאות מעתיקות את שני קובצי ההרצה.

```bash
# רק בפעם הראשונה, אם תיקיית היעד עדיין חסרה
ssh -x admin@192.168.0.29 'mkdir -p ~/sentinal-eye'

project_dir="/Users/bendamti/Documents/ChatGPT/Sentinal Eye"
scp "$project_dir/run.sh" "$project_dir/live_insightface.py" \
  "admin@192.168.0.29:/home/admin/sentinal-eye/"
```

לבדיקה אחרי ההעתקה, הרץ על ה־Jetson:

```bash
ls -l ~/sentinal-eye/run.sh ~/sentinal-eye/live_insightface.py
```

## בחירת מודל לסיווג הבעות פנים

`--emotion-backend hsemotion` מפעיל את `enet_b2_7` על הפנים ש־InsightFace זיהה ומוסיף את ההבעה ליד השם, למשל `Ben | Happy`. מודל ההבעות משתמש בהתקן שנבחר ב־`--device`. עבור אדם מהגלריה, הסקריפט משלב תוצאות טריות משתי זוויות הצילום לפי איכות התמונה ומחליק את התווית לאורך זמן. שם מודל הפנים ושם מודל ההבעות מופיעים בתחתית הווידאו ובפלט בתחילת ההרצה. [קובץ ה־ONNX הרשמי של HSEmotion](https://github.com/sb-ai-lab/EmotiEffLib/blob/main/models/affectnet_emotions/onnx/enet_b2_7.onnx)

העתקה חד־פעמית מה־Mac:

```bash
project_dir="/Users/bendamti/Documents/ChatGPT/Sentinal Eye"
scp "$project_dir/run.sh" "$project_dir/live_insightface.py" \
  "$project_dir/dan_emotions.py" "$project_dir/hsemotion_enet_b2_7.onnx" \
  "admin@192.168.0.29:/home/admin/sentinal-eye/"
```

להפעלה בדפדפן, עצור את ההרצה הקודמת והרץ מה־Mac:

```bash
ssh -x -t -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:8765:127.0.0.1:8765 admin@192.168.0.29 \
  'bash ~/sentinal-eye/run.sh --web --emotion-backend hsemotion'
```

כשתופיע `Web preview listening`, פתח את [תצוגת המצלמות](http://127.0.0.1:8765/). על מסך שמחובר ל־Jetson אפשר להריץ `bash ~/sentinal-eye/run.sh --preview --emotion-backend hsemotion`. להרצה עם פלט בטרמינל, השתמש ב־`bash ~/sentinal-eye/run.sh --emotion-backend hsemotion`.

להשוואה מול DAN, בחר `--emotion-backend dan` אחרי שתוודא שהקובץ `dan_affectnet7.onnx` נמצא בתיקיית הסקריפט ב־Jetson. גם הפקודה הקודמת עם `--emotion-model dan_affectnet7.onnx` ממשיכה לעבוד. [המודל הרשמי AffectNet-7 של DAN](https://github.com/yaoing/DAN#models)

## הוספת אנשים ותמונות ייחוס

ב־Mac, שמור ב־`~/Pictures/sentinal-people/` תיקייה לכל אדם. שם התיקייה הוא השם שמופיע בווידאו. השתמש באותיות לטיניות וב־3–5 תמונות ברורות לכל אדם, עם פנים של אדם אחד בכל תמונה. הקוד קורא JPG, JPEG, PNG ו־WebP.

```text
~/Pictures/sentinal-people/
├── Ben/
│   ├── front.jpg
│   └── side.jpg
└── Dana/
    ├── front.jpg
    └── side.jpg
```

```bash
# העתקת הגלריה בפעם הראשונה מה־Mac
scp -r "$HOME/Pictures/sentinal-people" \
  "admin@192.168.0.29:/home/admin/sentinal-eye/"

# הוספת אדם חדש אחרי שהגלריה כבר נמצאת על ה־Jetson
scp -r "$HOME/Pictures/sentinal-people/Dana" \
  "admin@192.168.0.29:/home/admin/sentinal-eye/sentinal-people/"
```

הפעל מחדש אחרי הוספה או שינוי. `Face gallery` מציג כמה אנשים ותמונות נטענו. המטמון המקומי חוסך עיבוד חוזר של תמונות מוכרות. כדי להוציא אדם מהגלריה, הסר את התיקייה שלו **ב־Jetson** והפעל מחדש.

### מצב ייחוס לאדם אחד

אם העלית את תיקיית התמונות `sentinal-me`, אפשר להריץ על ה־Jetson את מצב האדם היחיד:

```bash
cd ~/sentinal-eye
.venv/bin/python -u live_insightface.py --headless --device cuda \
  --model buffalo_m --reference-dir sentinal-me --name "Me"
```

## אפשרויות הרצה ומדידת ביצועים

אם רוצים וידאו מקומי או בדפדפן, כתוב `--preview` או `--web` מיד אחרי `run.sh`, ואז את יתר האפשרויות. אלה דוגמאות נפרדות להרצה ב־Jetson:

```bash
# בדיקה על ה־CPU
bash ~/sentinal-eye/run.sh --device cpu

# מצלמה אחת בלבד
bash ~/sentinal-eye/run.sh --sensor-id 0

# השוואה למודל הגדול יותר
bash ~/sentinal-eye/run.sh --model buffalo_l

# זיהוי פנים קטנות יותר בתמונה, במחיר של יותר עיבוד
bash ~/sentinal-eye/run.sh --det-size 480

# שינוי תקרת קצב הזיהוי לכל מצלמה
bash ~/sentinal-eye/run.sh --detect-fps 10

# העלאת סף ההתאמה אם מופיעות התאמות שגויות
bash ~/sentinal-eye/run.sh --match-threshold 0.55

# חלון מקומי עם איזון לבן לתאורת נורת להט
bash ~/sentinal-eye/run.sh --preview --wbmode 2
```

| אפשרות | משמעות |
| --- | --- |
| `--model buffalo_m` או `--model buffalo_l` | בחירת חבילת InsightFace. בשתיהן אותו מודל להתאמת זהות. הגלאי של `buffalo_m` קל יותר. |
| `--wbmode 1` | איזון לבן אוטומטי. |
| `--wbmode 2` | איזון לבן לתאורת נורת להט. |
| `--wbmode 3` | איזון לבן לתאורת פלורסנט. |
| `--wbmode 5` | איזון לבן לאור יום. |
| `--det-size 320`, `480` או `640` | רזולוציית הקלט לגלאי. ברירת המחדל `320`. |
| `--detect-fps` | תקרת בדיקות לשנייה לכל מצלמה. ברירת המחדל `5`. |
| `--match-threshold` | סף דמיון בין הפנים שנצפו לתמונות הייחוס. ברירת המחדל `0.50`. |

[חבילות המודלים של InsightFace](https://github.com/deepinsight/insightface/blob/master/model_zoo/README.md) · [מצבי איזון לבן ב־JetPack 7.2.1](https://docs.nvidia.com/jetson/archives/r39.2.1/DeveloperGuide/SD/Multimedia/AcceleratedGstreamer.html#csi-camera-runtime-commands)

בטרמינל נוסף שנותר מחובר ל־Jetson, הרץ:

```bash
tegrastats --interval 1000
```

`GR3D_FREQ` מציג פעילות GPU ו־`CPU [...]` מציג עומס לכל ליבה. פלט הסקריפט מציג `Face detector providers` בתחילת ההרצה ו־`Detection` למדידת קצב הזיהוי בפועל. [מדריך NVIDIA ל־tegrastats](https://docs.nvidia.com/jetson/archives/r39.2/DeveloperGuide/AT/JetsonLinuxDevelopmentTools/TegrastatsUtility.html)

## הקמה חד־פעמית על Jetson חדש

הפקודות להלן מיועדות ל־JetPack 7.2.1 עם Python 3.12. הרץ אותן על ה־Jetson.

```bash
mkdir -p ~/sentinal-eye
cd ~/sentinal-eye
sudo apt update
sudo apt install python3-venv python3-gi gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0
/usr/bin/python3 -m venv --system-site-packages .venv
source .venv/bin/activate
python -m pip install insightface==2.1
```

להפעלת CUDA, החלף את חבילת ONNX Runtime בגרסת GPU שתומכת ב־Jetson Orin:

```bash
python -m pip download --only-binary=:all: --no-deps -d /tmp onnxruntime-gpu==1.32.0 &&
python -m pip uninstall -y onnxruntime &&
python -m pip install /tmp/onnxruntime_gpu-1.32.0-cp312-cp312-manylinux_2_34_aarch64.whl
```

InsightFace מוריד את חבילת המודל שבחרת בהפעלה הראשונה. [התקנת InsightFace](https://github.com/deepinsight/insightface/tree/master/python-package#installation) · [גרסת ONNX Runtime 1.32.0](https://github.com/microsoft/onnxruntime/releases/tag/v1.32.0)

### בדיקת InsightFace על תמונה

הבדיקה מריצה את `buffalo_l` על ה־CPU עם תמונת הדוגמה של InsightFace ושומרת את התוצאה ב־`insightface-test.jpg`.

```bash
cd ~/sentinal-eye
source .venv/bin/activate
python - <<'PY'
import cv2
from insightface.app import FaceAnalysis
from insightface.data import get_image

app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
app.prepare(ctx_id=-1, det_size=(640, 640))
image = get_image("t1")
faces = app.get(image)
cv2.imwrite("insightface-test.jpg", app.draw_on(image, faces))
print(f"Detected faces: {len(faces)}")
PY
```

## בדיקות מצלמה ותקלות

### תצוגה מקומית של שתי המצלמות ב־GStreamer

עצור את `run.sh` ואז הרץ על ה־Jetson:

```bash
DISPLAY=:1 XAUTHORITY=/run/user/1000/gdm/Xauthority gst-launch-1.0 \
  nvarguscamerasrc sensor-id=0 ! 'video/x-raw(memory:NVMM),width=960,height=540,framerate=30/1' \
  ! queue ! nv3dsink window-x=0 window-y=0 sync=false \
  nvarguscamerasrc sensor-id=1 ! 'video/x-raw(memory:NVMM),width=960,height=540,framerate=30/1' \
  ! queue ! nv3dsink window-x=960 window-y=0 sync=false
```

### זיהוי מצלמות, Argus והגדרת CSI

```bash
# כתובת הרשת, דגם הלוח וגרסת מערכת ההפעלה
hostname -I
tr '\000' '\n' < /proc/device-tree/model
head -n 1 /etc/nv_tegra_release

# התקני וידאו ולוג חיישני CSI
ls -l /dev/video*
sudo journalctl -k -b --no-pager --grep='imx219|nvcsi|tegra-capture'

# תוכניות חומרה זמינות
sudo /opt/nvidia/jetson-io/config-by-hardware.py -l

# פתיחת ההגדרה Camera IMX219 Dual במידת הצורך
sudo /opt/nvidia/jetson-io/jetson-io.py

# בדיקת לכידה ממצלמה 0 בלי לפתוח חלון
gst-launch-1.0 -v nvarguscamerasrc sensor-id=0 num-buffers=10 ! fakesink silent=false

# שמירת תמונה ממצלמה 0 לתיקייה הנוכחית
gst-launch-1.0 nvarguscamerasrc sensor-id=0 num-buffers=1 \
  ! queue ! nvvidconv ! 'video/x-raw,format=I420' \
  ! jpegenc ! filesink location=test.jpg

# אם Argus נתקע, אחרי עצירת ההרצות על המצלמות
sudo systemctl restart nvargus-daemon
```

אם `No cameras available` מופיע וביומן יש `i2c read probe (-121)`, כבה את ה־Jetson ונתק חשמל לפני בדיקת כבלי הסרט ב־`CAM0` וב־`CAM1`. בדיקת דגם החיישן ממשיכה דרך `ls -l /dev/video*` אחרי ההדלקה. [מחברי ה־CSI במדריך NVIDIA](https://docs.nvidia.com/jetson/orin-nano-devkit/user-guide/hardware_layout.html)

### כשהווידאו לא מופיע ב־Mac

הרץ בטרמינל מקומי נוסף ב־Mac, בזמן שפקודת ה־SSH לשידור פתוחה:

```bash
lsof -nP -iTCP:8765 -sTCP:LISTEN
curl -fsS --max-time 3 http://127.0.0.1:8765/ -o /dev/null
```

בלי מאזין בפורט `8765`, בדוק את הפלט בטרמינל שבו הפעלת את ה־SSH. אם מופיע `unrecognized arguments: --web`, העתק מחדש מה־Mac את `run.sh` ואת `live_insightface.py` יחד. אם מופיע `Web preview listening` והעמוד נפתח בלי תמונה, בדוק אם הטרמינל מדווח על שגיאה ממצלמה מסוימת או על `Preview` ו־`Detection` אחרי חמש שניות.

לחיווי תצוגה מקומית על ה־Jetson, אפשר לבדוק גם את משתני שולחן העבודה:

```bash
echo "session=$XDG_SESSION_TYPE display=$DISPLAY wayland=$WAYLAND_DISPLAY"
```
