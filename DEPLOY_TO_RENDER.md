# 🚀 دليل نشر نظام "ميجا مول" على منصة Render (Render.com Deployment Guide)

تم تجهيز وفحص النظام بالكامل ليعمل بسلاسة 100% على السحابة (Cloud) ومنصة **Render.com**.

---

## 📋 نظرة عامة على الملفات المجهزة للنشر

يحتوي مجلد المشروع على جميع ملفات التكوين القياسية المطلوبة لـ Render:
1. `render.yaml` - ملف Blueprint التلقائي لإنشاء الخدمة وإعداد البيئة فورياً.
2. `Procfile` - أمر تشغيل خادم الويب السحابي:
   ```bash
   web: uvicorn server:app --host 0.0.0.0 --port $PORT
   ```
3. `runtime.txt` - تحديد إصدار بايثون القياسي (`python-3.11.9`).
4. `requirements.txt` - جميع الحزم البرمجية المطلوبة مع تثبيت الإصدارات المستقرة.
5. `Dockerfile` و `.dockerignore` - خيار النشر عبر الحاويات (Docker Container) إذا رغبت في ذلك.
6. `.gitignore` - يمنع رفع الملفات المؤقتة وذاكرة التخزين المؤقتة غير الضرورية.

---

## 🛠️ الطريقة الأولى: النشر عبر GitHub و Render Web Service (موصى بها)

### 1. رفع المشروع على GitHub
1. قم بإنشاء مستودع جديد (Repository) على حسابك في GitHub (مثلاً باسم: `mega-mall-system`).
2. يمكنك رفع محتويات مجلد المشروع (أو مجلد `deploy_clean`) إلى المستودع:
   ```bash
   git init
   git add .
   git commit -m "Initial production release for Mega Mall System"
   git branch -M main
   git remote add origin https://github.com/YOUR_USERNAME/mega-mall-system.git
   git push -u origin main
   ```

---

### 2. إنشاء الخدمة على Render.com
1. سجّل الدخول إلى موقع [Render Dashboard](https://dashboard.render.com).
2. اضغط على زر **New +** ثم اختر **Web Service**.
3. اختر **Build and deploy from a Git repository** واضغط **Next**.
4. اربط حساب GitHub واختر المستودع الذي رفعته `mega-mall-system`.
5. قم بتعبئة الإعدادات كالتالي:
   - **Name:** `mega-mall-system` (أو أي اسم تفضله)
   - **Region:** اختر أقرب منطقة لك (مثل `Frankfurt (EU Central)`)
   - **Branch:** `main`
   - **Runtime:** `Python 3`
   - **Build Command:**
     ```bash
     pip install -r requirements.txt
     ```
   - **Start Command:**
     ```bash
     uvicorn server:app --host 0.0.0.0 --port $PORT
     ```
   - **Instance Type:** اختر **Free** (أو Starter حسب رغبتك).

---

### 3. متغيرات البيئة (Environment Variables)
في تبويب **Environment Variables**، يمكنك إضافة المتغيرات التالية:
| المفتاح (Key) | القيمة الافتراضية (Value) | الوصف |
| :--- | :--- | :--- |
| `PYTHON_VERSION` | `3.11.9` | تحديد إصدار بايثون |
| `HOST` | `0.0.0.0` | السماح باستقبال الطلبات السحابية |

---

## 💾 الحفاظ على قاعدة البيانات (SQLite Persistence)

### أ. في الخطة المجانية (Free Tier):
* أقراص الخطة المجانية تكون *Ephemeral* (مؤقتة)، مما يعني أن البيانات المسجلة حديثاً قد تعود لحالة البداية إذا نام السيرفر وأعيد بناؤه بالكامل.
* **الحل المدمج الذكي:** النظام مزود بنظام **نسخ احتياطي فوري وتلقائي عبر تليجرام** (`auto_backup.py`). بمجرد تفعيل بوت تليجرام من لوحة الإدارة، يتم إرسال نسخة احتياطية دورية لك آلياً، كما يمكنك تحميل قاعدة البيانات بضغطة زر من تبويب النسخ الاحتياطي.

### ب. في الخطة المدفوعة (Render Persistent Disk - موصى به للإنتاج الدائم):
إذا أردت حفظاً دائماً لا يتأثر بأي إعادة تشغيل:
1. في إعدادات الخدمة على Render، توجه إلى قسم **Disks** واضغط **Add Disk**.
2. **Name:** `megamall_disk`
3. **Mount Path:** `/data`
4. **Size:** 1 GB (كافٍ جداً لملايين العمليات)
5. أضف متغير بيئة جديد:
   - **Key:** `DB_PATH`
   - **Value:** `/data/mega_mall.db`

---

## 🐳 الطريقة الثانية: النشر باستخدام Docker

إذا كنت تفضل استخدام Docker (سواء على Render أو Fly.io أو Railway أو سيرفر VPS خاص):
1. يحتوي المشروع على `Dockerfile` جاهز ومحسّن:
   ```bash
   docker build -t mega-mall:latest .
   docker run -d -p 8000:8000 --name mega_mall_app mega-mall:latest
   ```
2. على Render: اختر **Docker** كـ Runtime وسيقوم Render تلقائياً ببناء وتشغيل الحاوية.

---

## 🔑 بيانات تسجيل الدخول الافتراضية

| الحساب | اسم المستخدم (Username) | كلمة المرور (Password) | الصلاحية |
| :--- | :--- | :--- | :--- |
| **لوحة الإدارة الرئيسية** | `admin` | `Adm$Dfm0379` | مدير النظام الكامل (Admin) |
| **شركة كونكريت** | `concrete` | `12341234` | شركة (Company) |
| **شركة ترست** | `trust` | `Trs$jLP3211` | شركة (Company) |
| **شركة MMD** | `mmd` | `Mmd!iEg2427` | شركة (Company) |
| **شركة توب ديلز** | `td` | `Td!DOB0296` | شركة (Company) |

---

## ✅ فحوصات التأكيد بعد النشر

بمجرد اكتمال البناء ووصول الحالة إلى **Live**:
1. افتح رابط موقعك الممنوح من ريندر: `https://mega-mall-system.onrender.com`.
2. سجّل الدخول بحساب الـ `admin`.
3. تأكد من تحميل الوحدات (241 وحدة جاهزة).
4. جرب تسجيل عملية بيع جديدة وتصدير إيصال PDF لبيان الأقساط.
5. تحقق من سجل النشاطات الإداري (Activity Logs) وتصدير التقرير التنفيذي PDF.
