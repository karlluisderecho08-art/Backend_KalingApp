--
-- KalingApp backend -- FULL dump (schema + data) of a freshly built
-- development SQLite database, at Backend_KalingApp@256b7a0.
--
-- Built on 2026-10-02 by running, against an empty database:
--   manage.py migrate
--   manage.py seed_articles
--   manage.py seed_support_contacts
--   manage.py seed_facilities
-- and dumped with Python's sqlite3 Connection.iterdump().
--
-- It is NOT a dump of production. It contains no accounts, bookings,
-- questionnaires, chat messages or notifications -- only the schema
-- and the seed/reference data every deployment starts with:
--   articles_article (6), directory_supportcontact (4), milkbank_facility (4)
-- plus Django's own bookkeeping rows (permissions, content types,
-- applied migrations). No personal data and no credentials are in it.
--
-- 25 tables. Load into an empty SQLite database with:
--   sqlite3 kalingapp.sqlite3 < db_dump.sql
--
-- Production runs PostgreSQL, whose DDL differs (identity columns,
-- timestamptz, boolean): see docs/schema_dump.sql for that schema.
--
BEGIN TRANSACTION;
CREATE TABLE "accounts_pendingregistration" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "email" varchar(254) NOT NULL UNIQUE, "password" varchar(128) NOT NULL, "mom_name" varchar(150) NOT NULL, "baby_name" varchar(150) NOT NULL, "code" varchar(6) NOT NULL, "attempts" smallint unsigned NOT NULL CHECK ("attempts" >= 0), "created_at" datetime NOT NULL, "sent_at" datetime NULL);
CREATE TABLE "accounts_user" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "password" varchar(128) NOT NULL, "last_login" datetime NULL, "is_superuser" bool NOT NULL, "first_name" varchar(150) NOT NULL, "last_name" varchar(150) NOT NULL, "is_staff" bool NOT NULL, "is_active" bool NOT NULL, "date_joined" datetime NOT NULL, "email" varchar(254) NOT NULL UNIQUE, "role" varchar(20) NOT NULL, "mom_name" varchar(150) NOT NULL, "baby_name" varchar(150) NOT NULL, "baby_age_weeks" integer unsigned NULL CHECK ("baby_age_weeks" >= 0), "breastfeeding_status" varchar(255) NOT NULL, "baby_birth_date" date NULL, "pediatric_clinic" varchar(255) NOT NULL, "tracking_streaks" integer unsigned NOT NULL CHECK ("tracking_streaks" >= 0), "total_drawn_ml" integer unsigned NOT NULL CHECK ("total_drawn_ml" >= 0), "latitude" real NULL, "longitude" real NULL, "location_consent_given" bool NOT NULL, "location_consent_at" datetime NULL, "last_active_date" date NULL, "has_seen_walkthrough" bool NOT NULL, "email_verified" bool NOT NULL, "facility_id" bigint NULL REFERENCES "milkbank_facility" ("id") DEFERRABLE INITIALLY DEFERRED, "password_reset_attempts" smallint unsigned NOT NULL CHECK ("password_reset_attempts" >= 0), "password_reset_code" varchar(6) NOT NULL, "password_reset_sent_at" datetime NULL, "total_received_ml" integer unsigned NOT NULL CHECK ("total_received_ml" >= 0));
CREATE TABLE "accounts_user_groups" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "user_id" bigint NOT NULL REFERENCES "accounts_user" ("id") DEFERRABLE INITIALLY DEFERRED, "group_id" integer NOT NULL REFERENCES "auth_group" ("id") DEFERRABLE INITIALLY DEFERRED);
CREATE TABLE "accounts_user_user_permissions" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "user_id" bigint NOT NULL REFERENCES "accounts_user" ("id") DEFERRABLE INITIALLY DEFERRED, "permission_id" integer NOT NULL REFERENCES "auth_permission" ("id") DEFERRABLE INITIALLY DEFERRED);
CREATE TABLE "articles_article" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "title" varchar(255) NOT NULL, "category" varchar(50) NOT NULL, "read_time" varchar(50) NOT NULL, "teaser" varchar(500) NOT NULL, "content" text NOT NULL, "author" varchar(150) NOT NULL, "rating" varchar(20) NOT NULL, "evidence_label" varchar(255) NOT NULL, "date" varchar(50) NOT NULL);
INSERT INTO "articles_article" VALUES(1,'How to Breastfeed Correctly: Positioning and Latch','Latching Techniques','2 min read','The fundamentals of a comfortable, effective latch -- how to position your baby and recognize the signs that feeding is going well.','Good breastfeeding starts with the right position and a good latch. Getting these two things right helps the baby feed effectively and keeps the mother comfortable.

Positioning

Find a position that feels comfortable, whether you are sitting or lying down. Staying relaxed matters, because tension can temporarily lower milk flow. Hold the baby so that their ear, shoulder, and hip form a straight line, fully supported by your hands. The baby''s mouth should face the breast directly, with the nose in line with the nipple. Keep the baby close, tummy touching your body.

Latching

Guide the baby gently toward the breast and let their natural rooting reflex take over. You can tell the latch is good when the baby''s mouth is wide open, the lower lip is turned outward, and the chin touches or nearly touches the breast. Most of the areola should be inside the baby''s mouth, with more of it visible above the top lip than below the bottom lip.

Effective sucking

A well-latched baby sucks slowly and deeply, pausing now and then, with occasional swallowing. Staying calm and relaxed during the feed supports bonding and helps milk production.

General tips

Feed the baby whenever they are hungry, day and night. Night feeds are especially important because they stimulate milk supply. Frequent feeding on demand naturally increases the amount of milk your body makes as it responds to the baby''s needs. If a baby is too weak to suckle, expressing milk and feeding it to them is a safe alternative.

Source: UNICEF India, "How to Breastfeed Correctly." https://www.unicef.org/india/stories/how-breastfeed-correctly','Adapted from UNICEF (unicef.org)','4.9 ★','Organization-Verified & Peer-Reviewed','August 2025');
INSERT INTO "articles_article" VALUES(2,'5 Common Breastfeeding Problems and How to Handle Them','Latching Techniques','3 min read','From latch trouble to low supply, engorgement, cracked nipples, and blocked ducts -- practical guidance for the most common breastfeeding challenges.','Breastfeeding is not always easy. It takes time and practice for both mother and baby, and many mothers run into challenges along the way. The good news is that most problems can be managed with the right support. If you are struggling, reach out to a midwife, lactation specialist, or health care provider.

1. Getting a good latch

A proper latch is the foundation of comfortable, effective feeding. Position the baby''s nose opposite your nipple and let their head tilt back slightly so the top lip brushes the nipple, which prompts them to open wide. When the mouth is open, bring the baby quickly to the breast, chin leading, so they take a large mouthful of breast rather than just the nipple. Signs of a good latch: feeding feels comfortable with no pain, more areola shows above the mouth than below, the mouth is wide open, the lower lip turns outward, and the chin touches or nearly touches the breast.

2. Low milk supply

Worrying about producing enough is common, and there is usually an identifiable cause a provider can help you find. Frequent contributors include a delayed start to breastfeeding, little or no skin-to-skin contact, poor attachment, feeding on a fixed schedule, short or infrequent feeds, no nighttime feeding, and stress or fatigue. To protect your supply, begin skin-to-skin contact and breastfeeding as soon as possible after delivery, seek skilled support to confirm good attachment, room-in with your baby around the clock, and breastfeed exclusively unless a provider advises otherwise.

3. Engorged breasts

Full breasts feel hot, heavy, and hard, but milk still flows and there is no fever. Engorgement is different and more serious: the breast becomes painfully swollen, tight, shiny, and possibly red, milk stops flowing, and a fever may last around 24 hours. Engorgement is usually caused by too much milk sitting in the breast, a delayed start to feeding, poor attachment, or infrequent milk removal. Prevent it by starting breastfeeding soon after delivery, ensuring a good latch, feeding without restriction, and removing milk frequently by feeding, hand expression, or pumping. See a provider if engorgement sets in.

4. Cracked nipples

Sore or cracked nipples are usually a sign the baby is not attached properly, so the first fix is correcting the latch. If a nipple cracks or bleeds, contact your provider promptly. At home, dabbing a little expressed breast milk onto the nipple after feeds can soothe soreness, since breast milk has natural healing properties.

5. Blocked ducts and mastitis

Milk reaches the baby through a system of ducts, and sometimes part of that system becomes blocked, which is painful and stops milk from flowing freely. If a blocked duct does not clear, or milk builds up, the breast can become inflamed, a condition called mastitis. Causes include short or infrequent feeds, incomplete milk removal, damaged tissue, or bacteria entering through a cracked nipple. To relieve symptoms, improve milk removal, correct the latch, feed more often, massage gently toward the nipple, apply a warm compress, wear loose clothing, and vary feeding positions. See a provider if pain persists.

Breastfeeding is not a one-woman job. Bumps along the way are normal, so bring any concerns to your health care provider and lean on family and friends for support.

Source: UNICEF Kosovo Programme, "5 common breastfeeding problems." https://www.unicef.org/kosovoprogramme/5-common-breastfeeding-problems','Adapted from UNICEF (unicef.org)','4.9 ★','Organization-Verified & Peer-Reviewed','2026');
INSERT INTO "articles_article" VALUES(3,'Breastfeeding When You Return to Work','Milk Storage & Safety','4 min read','Practical tips for continuing to breastfeed after returning to work, from planning ahead to safely storing expressed milk.','Balancing work and family is hard at the best of times, and for breastfeeding mothers returning to work after giving birth it can feel overwhelming. Breastfeeding gives children the healthiest start in life: breast milk acts as a baby''s first vaccine, supports brain development, and protects the mother''s health. For this reason, UNICEF and WHO urge governments and employers to adopt family-friendly policies that give mothers the time, space, and support they need to keep breastfeeding. The tips below can make the transition back to work a little easier.

Plan ahead

Let your family, friends, and co-workers know about your decision to breastfeed, and look for breastfeeding support groups who can help you find solutions along the way. Mothers continue breastfeeding while working in several ways: keeping the baby with them and feeding throughout the day; going home to breastfeed, or having someone bring the baby to them, if work is nearby; using a nearby day care where they can feed during the workday; or, when feeding during work hours is not possible, learning to express milk and leaving a supply for a caregiver.

Breastfeeding support at work

Find out what your employer offers, such as on-site childcare or the option to bring your baby to work. At a minimum, employers must comply with existing laws on maternity leave and workplace breastfeeding support, so check what is required where you live. Ask about your workplace lactation policy and whether there is a lactation room and how it works. If there is none, talk to your manager or HR about arranging a private space, and consider sharing UNICEF''s recommendations for employers.

Rather than returning straight to a full five-day week, ask whether your schedule can be flexible at first. A gradual return, temporary part-time work, or teleworking can all ease the disruption to your breastfeeding routine. If you work nights, ask whether you can be reassigned to a morning shift so you can breastfeed directly at night. Remember that your employer is responsible for your safety at work and must not discriminate on the basis of pregnancy, breastfeeding, or family status.

What a lactation room should look like

A lactation or breastfeeding room should be a clean, comfortable, safe, and private space where mothers can breastfeed, express milk, and store it properly. Ideally it includes a cold storage system, preferably a fridge or freezer for the room''s exclusive use, along with handwashing facilities and supplies such as drinking water, liquid soap, hand sanitizer, surface cleaner, and paper towels. It should have a comfortable, individual chair with adjustable height and good back support, made of a material that is easy to wash and disinfect, rather than a sofa.

The room should sit in a physically separate area close to your workspace and away from toilets, directly accessible and fully available during the workday. Entrances must close properly so that mothers are not visible from outside, and only breastfeeding women and cleaning staff should have access. Natural light is preferred, but where that is not possible, suitable artificial lighting, ventilation, and heating must be provided, with air-conditioning recommended for comfortable temperatures. Cleaning should use odourless, food-safe products reserved for the room, and walls, floors, and furniture should have smooth, washable surfaces, avoiding carpets, fabric curtains, or anything else that traps dust.

Checklist before returning to work

Plan out your workday and build in your expressing sessions, using alarms or calendar reminders to protect that time. Do a trial run of the new schedule shortly before you return so you can anticipate challenges and find solutions. Things to remember to pack: nursing pads or cloths to prevent milk stains, milk storage containers, labels and a pen, an insulated cooler with frozen ice or gel packs, and, if you use a pump, the materials needed to clean it.

Breast milk can be stored in clean glass or hard BPA-free plastic bottles with tight lids, or in milk storage bags made for freezing. Wash containers with warm, soapy water. Always label each container with the date and time, using something that will not smudge when wet, such as a permanent marker on masking tape, especially if the milk will be frozen.

Collecting and storing breast milk at work

Try to express as often as you would normally feed your baby, and seal the milk in labeled containers marked with the date and time of expression. Keep them in a refrigerator or an insulated cooler with frozen gel or ice packs. In a refrigerator, store milk at the back where the temperature stays most constant, never in the door, because the temperature there rises each time the door opens and raises the risk of bacterial growth.

Transporting and storing breast milk at home

Carry the expressed milk home in your insulated cooler. Storage guidance can vary by country, so check with your national health authorities. As a general approach, WHO and UNICEF recommend:

- Freshly expressed milk to be used within 24 hours is best kept at the back of the refrigerator, where the temperature is most stable.
- Milk that will not be used within 24 hours keeps longer if frozen. Freezing in small volumes is more practical and avoids repeated freezing and thawing or wastage.
- If there is no refrigerator, expressed breast milk can be kept for about 8 hours at room temperature.

Breastfeed directly whenever you can

Expressing at work and breastfeeding directly whenever possible helps you keep making milk. When milk is not removed regularly, it can lead to plugged ducts, mastitis, and a decreased supply. Feed your baby directly before you leave for work and again when you get home, and ask the caregiver not to give a full feeding in the hour before you return, so your baby is ready to nurse. As much as possible, do not skip direct night feeds, since they help sustain milk production.

Returning to work after having a baby is not easy, and there may be moments when it all feels like too much. Remember that this period is temporary and adjusting takes time. Breastfeeding is not a one-woman job, so do not hesitate to ask family, friends, and colleagues for help.

Source: UNICEF Parenting, "Breastfeeding when you return to work." https://www.unicef.org/parenting/food-nutrition/breastfeeding-workplace','Adapted from UNICEF (unicef.org)','4.9 ★','Organization-Verified & Peer-Reviewed','2026');
INSERT INTO "articles_article" VALUES(4,'Nutrition During Pregnancy and Breastfeeding','Maternal Nutrition','3 min read','What to eat before, during, and after pregnancy to stay healthy and support your baby''s growth, from folate-rich foods to daily food group guidelines.','Good eating habits before and during pregnancy help keep a mother healthy and allow her baby to grow and develop properly. A woman''s need for energy and most nutrients rises during pregnancy and breastfeeding, so a healthy, balanced diet that meets those needs is important throughout this time.

Before pregnancy

A woman''s health before pregnancy affects both her ability to conceive and the health of her future baby, so being in good health at a healthy body weight matters. Being too thin or too heavy raises the risk of complications. Folate is especially important in the early stages of pregnancy, because too little of it can lead to serious birth defects. Women who could become pregnant are encouraged to eat plenty of folate-rich foods each day, particularly leafy green vegetables, beans, peas and other legumes, and liver. If diet alone cannot meet folate needs, folic-acid-fortified foods or supplements may help. Always consult a doctor or health professional before taking any vitamin or mineral supplements.

During pregnancy

Every pregnant woman needs a good, balanced diet and should gain weight to support a healthy pregnancy and delivery. It is not necessary to "eat for two"; a woman starting pregnancy at a healthy weight generally needs only about 280 extra calories a day. During pregnancy the mother''s own nutrient stores can run low, which raises her risk of illness, and a baby who does not get enough nutrition before birth is more likely to have health and development problems later.

Several nutrients matter especially: protein, to build new tissue, blood, cells, and bone; iron, whose needs are high and often require supplements; iodine, which helps prevent serious birth defects affecting the brain; folate, to prevent birth defects in the first weeks; and zinc and vitamins A and C. A helpful daily pattern is around four glasses of milk or milk products, three portions of meat, fish, eggs, or beans, four portions of fruit and vegetables, six portions of bread and cereals, and plenty of fluids. Pregnant women should avoid alcohol and have regular medical check-ups.

During breastfeeding

Breastfeeding also demands extra energy and nutrients, because the mother must replace what she passes to her baby through her milk. If her diet does not meet these needs, her baby will draw on her own stores, putting her health at risk and possibly affecting the baby''s development. The important nutrients are the same as in pregnancy: protein, zinc, calcium, vitamins A and C, iron, and folate. Extra servings of milk and high-protein snacks between meals, or one additional small meal a day, are good ways to meet these needs. A breastfeeding mother needs a varied, nutritious diet built on staple foods, vegetables, legumes, meat and fish, and plenty of fruit, along with plenty of water, milk, and other fluids. Because it can take two to three years after breastfeeding ends for a mother''s nutrient stores to fully recover, spacing pregnancies well apart supports the health of both the mother and her future babies.

Weight gain during pregnancy

All pregnant women need to gain weight, regardless of their weight before pregnancy, to support the growing baby and the added growth of the uterus, breasts, blood, and other tissues. As a general guide: a woman at a healthy weight should gain roughly 11.5 to 16 kg; an underweight woman, about 12.5 to 18 kg; an overweight woman, about 7 to 11.5 kg; and an obese woman, about 5 to 9 kg. A health professional can give guidance suited to each woman''s situation.

Source: Food and Agriculture Organization of the United Nations (FAO), "Nutrition during pregnancy and breastfeeding," from the FAO nutrition education fact sheets. https://www.fao.org/4/i3261e/i3261e08.pdf','Adapted from the Food and Agriculture Organization of the United Nations (FAO)','4.9 ★','Organization-Verified & Peer-Reviewed','2026');
INSERT INTO "articles_article" VALUES(5,'Unang Yakap (First Embrace): Encouraging Breastfeeding from the Start','Newborn Health','2 min read','The Philippine DOH''s four-step protocol for the first moments after birth, designed to give breastfeeding the strongest possible start.','Unang Yakap, or "First Embrace," is a campaign of the Philippines'' Department of Health (DOH), in cooperation with the World Health Organization (WHO), to put Essential Intrapartum Newborn Care (EINC) into practice. EINC is a set of simple, evidence-based steps performed right after birth that give both mother and newborn the best possible start. Public and private hospitals in the Philippines have been directed to follow the Unang Yakap protocol since 2009.

The four core steps

EINC follows four time-bound steps immediately after delivery:

1. Immediate and thorough drying of the baby.
2. Early skin-to-skin contact between the mother and the newborn.
3. Properly timed clamping of the umbilical cord.
4. Non-separation of mother and baby, so that breastfeeding can begin early.

Why early breastfeeding matters

The main goal of Unang Yakap is to support breastfeeding from the very first moments of life. When the baby is placed directly on the mother''s breast, skin to skin, the baby becomes familiar with the breast as the source of food. Rather than forcing the baby to latch, caregivers watch for the baby''s own feeding cues, such as tonguing, licking, and opening of the mouth, and gently guide the baby to the breast when those cues appear. This respects the baby''s natural readiness to feed.

Breastfeeding is encouraged within the first 60 to 90 minutes after birth. This early window matters because it is when colostrum, the protein- and nutrient-rich first milk, is released, giving the newborn important protection against infection.

To protect breastfeeding success, formula milk should not be given, and teats or pacifiers should be avoided to prevent nipple confusion. The mother''s breast should be the baby''s first and only source of food.

For whom

Unang Yakap is routine for low-risk births, but babies of higher-risk pregnancies can benefit from it just as much. It is intended for all births unless a specific circumstance prevents the full four steps, such as a baby who is not breathing even after immediate and thorough drying.

Source: World Health Organization Philippines, "Unang Yakap: Encouraging Breastfeeding from the Start." https://www.who.int/philippines/news/feature-stories/detail/unang-yakap-encouraging-breastfeeding-from-the-start','Adapted from World Health Organization (who.int)','4.9 ★','Organization-Verified & Peer-Reviewed','2026');
INSERT INTO "articles_article" VALUES(6,'Why Breastfeeding Matters for Your Newborn','Newborn Health','3 min read','How breast milk protects your baby''s health in the first six months, and what your options are if breastfeeding isn''t possible.','Breast milk gives a newborn everything they need for the first six months of life. Exclusive breastfeeding means feeding a baby only breast milk during this period, with no other food or drink, not even water. It is safe, clean, and always ready, even in places where clean water is hard to find.

Beginning breastfeeding right after birth, with skin-to-skin contact, helps keep the baby warm, strengthens their immune system, builds the bond between mother and child, and supports the mother''s milk supply over time. The very first milk, called colostrum, is rich in antibodies that protect the newborn against illness and infection. Because of this, breast milk works almost like a personalized medicine that adjusts to each baby''s needs.

Breastfed babies tend to have fewer ear infections, less diarrhea, and a lower chance of pneumonia and other common childhood illnesses. When a mother catches an illness, her body produces antibodies in her milk that help shield the baby from the same infection. Breastfeeding is also affordable for families and gentler on the environment than formula, which requires manufacturing, packaging, and transport.

Many mothers worry in the first few days that they are not producing enough milk. In most cases, the small amount of colostrum is exactly right for a newborn''s tiny stomach. No water, juice, or formula is needed at this stage. The most reliable way to establish breastfeeding is early and frequent contact: placing the baby at the breast soon after birth, when the instinct to suckle is strongest, ideally with support from a health worker, midwife, or lactation counselor.

Introducing a bottle or formula too early can interfere with the baby''s natural sucking reflex and is a common reason breastfeeding does not take hold. When a mother is truly unable to breastfeed, the best alternatives depend on her situation and may include her own expressed milk, screened milk from a healthy donor or a human-milk bank, or a breast-milk substitute given by cup rather than bottle.

Mothers should never be shamed or made to feel guilty if they cannot breastfeed, whether by choice or necessity. What matters most is that every baby is fed safely and that every mother has access to accurate information and skilled, non-judgmental support.

Source: UNICEF, "Why breastfeeding is critical for babies." https://www.unicef.org/stories/why-breastfeeding-best-babies','Adapted from UNICEF (unicef.org)','4.9 ★','Organization-Verified & Peer-Reviewed','May 2026');
CREATE TABLE "articles_articlecomment" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "text" text NOT NULL, "created_at" datetime NOT NULL, "is_reported" bool NOT NULL, "report_reason" varchar(20) NOT NULL, "article_id" bigint NOT NULL REFERENCES "articles_article" ("id") DEFERRABLE INITIALLY DEFERRED, "author_id" bigint NOT NULL REFERENCES "accounts_user" ("id") DEFERRABLE INITIALLY DEFERRED);
CREATE TABLE "articles_resourcelink" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "title" varchar(255) NOT NULL, "description" varchar(500) NOT NULL, "url" varchar(200) NOT NULL, "type" varchar(20) NOT NULL);
CREATE TABLE "auth_group" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "name" varchar(150) NOT NULL UNIQUE);
CREATE TABLE "auth_group_permissions" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "group_id" integer NOT NULL REFERENCES "auth_group" ("id") DEFERRABLE INITIALLY DEFERRED, "permission_id" integer NOT NULL REFERENCES "auth_permission" ("id") DEFERRABLE INITIALLY DEFERRED);
CREATE TABLE "auth_permission" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "content_type_id" integer NOT NULL REFERENCES "django_content_type" ("id") DEFERRABLE INITIALLY DEFERRED, "codename" varchar(100) NOT NULL, "name" varchar(255) NOT NULL);
INSERT INTO "auth_permission" VALUES(1,1,'add_logentry','Can add log entry');
INSERT INTO "auth_permission" VALUES(2,1,'change_logentry','Can change log entry');
INSERT INTO "auth_permission" VALUES(3,1,'delete_logentry','Can delete log entry');
INSERT INTO "auth_permission" VALUES(4,1,'view_logentry','Can view log entry');
INSERT INTO "auth_permission" VALUES(5,3,'add_permission','Can add permission');
INSERT INTO "auth_permission" VALUES(6,3,'change_permission','Can change permission');
INSERT INTO "auth_permission" VALUES(7,3,'delete_permission','Can delete permission');
INSERT INTO "auth_permission" VALUES(8,3,'view_permission','Can view permission');
INSERT INTO "auth_permission" VALUES(9,2,'add_group','Can add group');
INSERT INTO "auth_permission" VALUES(10,2,'change_group','Can change group');
INSERT INTO "auth_permission" VALUES(11,2,'delete_group','Can delete group');
INSERT INTO "auth_permission" VALUES(12,2,'view_group','Can view group');
INSERT INTO "auth_permission" VALUES(13,4,'add_contenttype','Can add content type');
INSERT INTO "auth_permission" VALUES(14,4,'change_contenttype','Can change content type');
INSERT INTO "auth_permission" VALUES(15,4,'delete_contenttype','Can delete content type');
INSERT INTO "auth_permission" VALUES(16,4,'view_contenttype','Can view content type');
INSERT INTO "auth_permission" VALUES(17,5,'add_session','Can add session');
INSERT INTO "auth_permission" VALUES(18,5,'change_session','Can change session');
INSERT INTO "auth_permission" VALUES(19,5,'delete_session','Can delete session');
INSERT INTO "auth_permission" VALUES(20,5,'view_session','Can view session');
INSERT INTO "auth_permission" VALUES(21,6,'add_auditlogentry','Can add audit log entry');
INSERT INTO "auth_permission" VALUES(22,6,'change_auditlogentry','Can change audit log entry');
INSERT INTO "auth_permission" VALUES(23,6,'delete_auditlogentry','Can delete audit log entry');
INSERT INTO "auth_permission" VALUES(24,6,'view_auditlogentry','Can view audit log entry');
INSERT INTO "auth_permission" VALUES(25,8,'add_user','Can add user');
INSERT INTO "auth_permission" VALUES(26,8,'change_user','Can change user');
INSERT INTO "auth_permission" VALUES(27,8,'delete_user','Can delete user');
INSERT INTO "auth_permission" VALUES(28,8,'view_user','Can view user');
INSERT INTO "auth_permission" VALUES(29,7,'add_pendingregistration','Can add pending registration');
INSERT INTO "auth_permission" VALUES(30,7,'change_pendingregistration','Can change pending registration');
INSERT INTO "auth_permission" VALUES(31,7,'delete_pendingregistration','Can delete pending registration');
INSERT INTO "auth_permission" VALUES(32,7,'view_pendingregistration','Can view pending registration');
INSERT INTO "auth_permission" VALUES(33,9,'add_article','Can add article');
INSERT INTO "auth_permission" VALUES(34,9,'change_article','Can change article');
INSERT INTO "auth_permission" VALUES(35,9,'delete_article','Can delete article');
INSERT INTO "auth_permission" VALUES(36,9,'view_article','Can view article');
INSERT INTO "auth_permission" VALUES(37,10,'add_articlecomment','Can add article comment');
INSERT INTO "auth_permission" VALUES(38,10,'change_articlecomment','Can change article comment');
INSERT INTO "auth_permission" VALUES(39,10,'delete_articlecomment','Can delete article comment');
INSERT INTO "auth_permission" VALUES(40,10,'view_articlecomment','Can view article comment');
INSERT INTO "auth_permission" VALUES(41,11,'add_resourcelink','Can add resource link');
INSERT INTO "auth_permission" VALUES(42,11,'change_resourcelink','Can change resource link');
INSERT INTO "auth_permission" VALUES(43,11,'delete_resourcelink','Can delete resource link');
INSERT INTO "auth_permission" VALUES(44,11,'view_resourcelink','Can view resource link');
INSERT INTO "auth_permission" VALUES(45,12,'add_supportcontact','Can add support contact');
INSERT INTO "auth_permission" VALUES(46,12,'change_supportcontact','Can change support contact');
INSERT INTO "auth_permission" VALUES(47,12,'delete_supportcontact','Can delete support contact');
INSERT INTO "auth_permission" VALUES(48,12,'view_supportcontact','Can view support contact');
INSERT INTO "auth_permission" VALUES(49,14,'add_facility','Can add facility');
INSERT INTO "auth_permission" VALUES(50,14,'change_facility','Can change facility');
INSERT INTO "auth_permission" VALUES(51,14,'delete_facility','Can delete facility');
INSERT INTO "auth_permission" VALUES(52,14,'view_facility','Can view facility');
INSERT INTO "auth_permission" VALUES(53,15,'add_milkbankrequest','Can add milk bank request');
INSERT INTO "auth_permission" VALUES(54,15,'change_milkbankrequest','Can change milk bank request');
INSERT INTO "auth_permission" VALUES(55,15,'delete_milkbankrequest','Can delete milk bank request');
INSERT INTO "auth_permission" VALUES(56,15,'view_milkbankrequest','Can view milk bank request');
INSERT INTO "auth_permission" VALUES(57,16,'add_transactionrecord','Can add transaction record');
INSERT INTO "auth_permission" VALUES(58,16,'change_transactionrecord','Can change transaction record');
INSERT INTO "auth_permission" VALUES(59,16,'delete_transactionrecord','Can delete transaction record');
INSERT INTO "auth_permission" VALUES(60,16,'view_transactionrecord','Can view transaction record');
INSERT INTO "auth_permission" VALUES(61,13,'add_donorquestionnaire','Can add donor questionnaire');
INSERT INTO "auth_permission" VALUES(62,13,'change_donorquestionnaire','Can change donor questionnaire');
INSERT INTO "auth_permission" VALUES(63,13,'delete_donorquestionnaire','Can delete donor questionnaire');
INSERT INTO "auth_permission" VALUES(64,13,'view_donorquestionnaire','Can view donor questionnaire');
INSERT INTO "auth_permission" VALUES(65,17,'add_notificationitem','Can add notification item');
INSERT INTO "auth_permission" VALUES(66,17,'change_notificationitem','Can change notification item');
INSERT INTO "auth_permission" VALUES(67,17,'delete_notificationitem','Can delete notification item');
INSERT INTO "auth_permission" VALUES(68,17,'view_notificationitem','Can view notification item');
INSERT INTO "auth_permission" VALUES(69,19,'add_chatsession','Can add chat session');
INSERT INTO "auth_permission" VALUES(70,19,'change_chatsession','Can change chat session');
INSERT INTO "auth_permission" VALUES(71,19,'delete_chatsession','Can delete chat session');
INSERT INTO "auth_permission" VALUES(72,19,'view_chatsession','Can view chat session');
INSERT INTO "auth_permission" VALUES(73,18,'add_chatmessage','Can add chat message');
INSERT INTO "auth_permission" VALUES(74,18,'change_chatmessage','Can change chat message');
INSERT INTO "auth_permission" VALUES(75,18,'delete_chatmessage','Can delete chat message');
INSERT INTO "auth_permission" VALUES(76,18,'view_chatmessage','Can view chat message');
INSERT INTO "auth_permission" VALUES(77,21,'add_knowledgedocument','Can add knowledge document');
INSERT INTO "auth_permission" VALUES(78,21,'change_knowledgedocument','Can change knowledge document');
INSERT INTO "auth_permission" VALUES(79,21,'delete_knowledgedocument','Can delete knowledge document');
INSERT INTO "auth_permission" VALUES(80,21,'view_knowledgedocument','Can view knowledge document');
INSERT INTO "auth_permission" VALUES(81,20,'add_knowledgechunk','Can add knowledge chunk');
INSERT INTO "auth_permission" VALUES(82,20,'change_knowledgechunk','Can change knowledge chunk');
INSERT INTO "auth_permission" VALUES(83,20,'delete_knowledgechunk','Can delete knowledge chunk');
INSERT INTO "auth_permission" VALUES(84,20,'view_knowledgechunk','Can view knowledge chunk');
CREATE TABLE "chat_chatmessage" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "text" text NOT NULL, "is_user" bool NOT NULL, "is_system_notice" bool NOT NULL, "created_at" datetime NOT NULL, "session_id" bigint NOT NULL REFERENCES "chat_chatsession" ("id") DEFERRABLE INITIALLY DEFERRED);
CREATE TABLE "chat_chatsession" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "prompt_count" integer unsigned NOT NULL CHECK ("prompt_count" >= 0), "token_count" integer unsigned NOT NULL CHECK ("token_count" >= 0), "owner_id" bigint NOT NULL UNIQUE REFERENCES "accounts_user" ("id") DEFERRABLE INITIALLY DEFERRED);
CREATE TABLE "chat_knowledgechunk" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "source_kind" varchar(20) NOT NULL, "source_id" integer unsigned NOT NULL CHECK ("source_id" >= 0), "source_title" varchar(255) NOT NULL, "source_reference" varchar(500) NOT NULL, "ordinal" integer unsigned NOT NULL CHECK ("ordinal" >= 0), "text" text NOT NULL, "embedding" text NOT NULL CHECK ((JSON_VALID("embedding") OR "embedding" IS NULL)), "embedding_model" varchar(100) NOT NULL, "source_revision" varchar(64) NOT NULL, "indexed_at" datetime NOT NULL, CONSTRAINT "unique_chunk_per_source_position" UNIQUE ("source_kind", "source_id", "ordinal"));
CREATE TABLE "chat_knowledgedocument" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "title" varchar(255) NOT NULL, "citation" varchar(500) NOT NULL, "content" text NOT NULL, "is_active" bool NOT NULL, "added_at" datetime NOT NULL, "updated_at" datetime NOT NULL);
CREATE TABLE "core_auditlogentry" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "action" varchar(100) NOT NULL, "target" varchar(255) NOT NULL, "created_at" datetime NOT NULL, "actor_id" bigint NULL REFERENCES "accounts_user" ("id") DEFERRABLE INITIALLY DEFERRED);
CREATE TABLE "directory_supportcontact" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "name" varchar(255) NOT NULL, "description" text NOT NULL, "phone" varchar(50) NOT NULL, "address" varchar(500) NOT NULL, "email" varchar(254) NOT NULL);
INSERT INTO "directory_supportcontact" VALUES(1,'Quezon City General Hospital','Quezon City government hospital and home of the Quezon City Human Milk Bank (2nd floor). For the milk bank, call the hospital and ask for local 209, or call 0960-363-6492. Milk bank hours: daily 8:00 AM - 5:00 PM, holidays 8:00 AM - 12:00 NN.','(02) 8863-0800','Seminary Road, Brgy. Bahay Toro, Project 8, Quezon City','humanmilkbank.qcgh@gmail.com');
INSERT INTO "directory_supportcontact" VALUES(2,'St. Luke''s Medical Center - Quezon City','Private hospital with breastfeeding support services and a human milk bank. Call the trunk line and ask for lactation support; customer service is on local 4122.','(02) 8723-0101','279 E. Rodriguez Sr. Ave., Quezon City 1112','customer.qc@stlukes.com.ph');
INSERT INTO "directory_supportcontact" VALUES(3,'Dr. Jose Fabella Memorial Hospital Human Milk Bank','Government-accredited human milk bank.','(02) 8866-7960','San Lazaro Compound, Tayuman St., Santa Cruz, Manila','mcc@fabella.doh.gov.ph');
INSERT INTO "directory_supportcontact" VALUES(4,'Arugaan','Volunteer-run breastfeeding and milk-banking advocacy organization.','','2 Starlight Street corner Vista Street, SSS Village, Marikina City, Metro Manila','arugaan.breastfeeding@gmail.com');
CREATE TABLE "django_admin_log" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "object_id" text NULL, "object_repr" varchar(200) NOT NULL, "action_flag" smallint unsigned NOT NULL CHECK ("action_flag" >= 0), "change_message" text NOT NULL, "content_type_id" integer NULL REFERENCES "django_content_type" ("id") DEFERRABLE INITIALLY DEFERRED, "user_id" bigint NOT NULL REFERENCES "accounts_user" ("id") DEFERRABLE INITIALLY DEFERRED, "action_time" datetime NOT NULL);
CREATE TABLE "django_content_type" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "app_label" varchar(100) NOT NULL, "model" varchar(100) NOT NULL);
INSERT INTO "django_content_type" VALUES(1,'admin','logentry');
INSERT INTO "django_content_type" VALUES(2,'auth','group');
INSERT INTO "django_content_type" VALUES(3,'auth','permission');
INSERT INTO "django_content_type" VALUES(4,'contenttypes','contenttype');
INSERT INTO "django_content_type" VALUES(5,'sessions','session');
INSERT INTO "django_content_type" VALUES(6,'core','auditlogentry');
INSERT INTO "django_content_type" VALUES(7,'accounts','pendingregistration');
INSERT INTO "django_content_type" VALUES(8,'accounts','user');
INSERT INTO "django_content_type" VALUES(9,'articles','article');
INSERT INTO "django_content_type" VALUES(10,'articles','articlecomment');
INSERT INTO "django_content_type" VALUES(11,'articles','resourcelink');
INSERT INTO "django_content_type" VALUES(12,'directory','supportcontact');
INSERT INTO "django_content_type" VALUES(13,'milkbank','donorquestionnaire');
INSERT INTO "django_content_type" VALUES(14,'milkbank','facility');
INSERT INTO "django_content_type" VALUES(15,'milkbank','milkbankrequest');
INSERT INTO "django_content_type" VALUES(16,'milkbank','transactionrecord');
INSERT INTO "django_content_type" VALUES(17,'notifications','notificationitem');
INSERT INTO "django_content_type" VALUES(18,'chat','chatmessage');
INSERT INTO "django_content_type" VALUES(19,'chat','chatsession');
INSERT INTO "django_content_type" VALUES(20,'chat','knowledgechunk');
INSERT INTO "django_content_type" VALUES(21,'chat','knowledgedocument');
CREATE TABLE "django_migrations" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "app" varchar(255) NOT NULL, "name" varchar(255) NOT NULL, "applied" datetime NOT NULL);
INSERT INTO "django_migrations" VALUES(1,'milkbank','0001_initial','2026-10-01 18:30:20.341498');
INSERT INTO "django_migrations" VALUES(2,'contenttypes','0001_initial','2026-10-01 18:30:20.350618');
INSERT INTO "django_migrations" VALUES(3,'contenttypes','0002_remove_content_type_name','2026-10-01 18:30:20.364556');
INSERT INTO "django_migrations" VALUES(4,'auth','0001_initial','2026-10-01 18:30:20.381640');
INSERT INTO "django_migrations" VALUES(5,'auth','0002_alter_permission_name_max_length','2026-10-01 18:30:20.392385');
INSERT INTO "django_migrations" VALUES(6,'auth','0003_alter_user_email_max_length','2026-10-01 18:30:20.400559');
INSERT INTO "django_migrations" VALUES(7,'auth','0004_alter_user_username_opts','2026-10-01 18:30:20.408452');
INSERT INTO "django_migrations" VALUES(8,'auth','0005_alter_user_last_login_null','2026-10-01 18:30:20.416766');
INSERT INTO "django_migrations" VALUES(9,'auth','0006_require_contenttypes_0002','2026-10-01 18:30:20.422247');
INSERT INTO "django_migrations" VALUES(10,'auth','0007_alter_validators_add_error_messages','2026-10-01 18:30:20.430642');
INSERT INTO "django_migrations" VALUES(11,'auth','0008_alter_user_username_max_length','2026-10-01 18:30:20.439652');
INSERT INTO "django_migrations" VALUES(12,'auth','0009_alter_user_last_name_max_length','2026-10-01 18:30:20.448294');
INSERT INTO "django_migrations" VALUES(13,'auth','0010_alter_group_name_max_length','2026-10-01 18:30:20.458291');
INSERT INTO "django_migrations" VALUES(14,'auth','0011_update_proxy_permissions','2026-10-01 18:30:20.467050');
INSERT INTO "django_migrations" VALUES(15,'auth','0012_alter_user_first_name_max_length','2026-10-01 18:30:20.474694');
INSERT INTO "django_migrations" VALUES(16,'accounts','0001_initial','2026-10-01 18:30:20.491323');
INSERT INTO "django_migrations" VALUES(17,'milkbank','0002_milkbankrequest_transactionrecord','2026-10-01 18:30:20.515082');
INSERT INTO "django_migrations" VALUES(18,'milkbank','0003_alter_facility_booked_count','2026-10-01 18:30:20.523628');
INSERT INTO "django_migrations" VALUES(19,'milkbank','0004_donorquestionnaire','2026-10-01 18:30:20.537697');
INSERT INTO "django_migrations" VALUES(20,'accounts','0002_user_last_active_date','2026-10-01 18:30:20.549960');
INSERT INTO "django_migrations" VALUES(21,'accounts','0003_user_has_seen_walkthrough','2026-10-01 18:30:20.563415');
INSERT INTO "django_migrations" VALUES(22,'accounts','0004_user_email_verification_attempts_and_more','2026-10-01 18:30:20.599976');
INSERT INTO "django_migrations" VALUES(23,'accounts','0005_user_facility','2026-10-01 18:30:20.619400');
INSERT INTO "django_migrations" VALUES(24,'accounts','0006_user_password_reset_attempts_and_more','2026-10-01 18:30:20.653786');
INSERT INTO "django_migrations" VALUES(25,'accounts','0007_pendingregistration_and_more','2026-10-01 18:30:20.683727');
INSERT INTO "django_migrations" VALUES(26,'accounts','0008_alter_pendingregistration_code_and_more','2026-10-01 18:30:20.693714');
INSERT INTO "django_migrations" VALUES(27,'accounts','0009_user_total_received_ml','2026-10-01 18:30:20.709862');
INSERT INTO "django_migrations" VALUES(28,'admin','0001_initial','2026-10-01 18:30:20.754471');
INSERT INTO "django_migrations" VALUES(29,'admin','0002_logentry_remove_auto_add','2026-10-01 18:30:20.770954');
INSERT INTO "django_migrations" VALUES(30,'admin','0003_logentry_add_action_flag_choices','2026-10-01 18:30:20.782412');
INSERT INTO "django_migrations" VALUES(31,'articles','0001_initial','2026-10-01 18:30:20.804392');
INSERT INTO "django_migrations" VALUES(32,'articles','0002_resourcelink','2026-10-01 18:30:20.811359');
INSERT INTO "django_migrations" VALUES(33,'chat','0001_initial','2026-10-01 18:30:20.841802');
INSERT INTO "django_migrations" VALUES(34,'chat','0002_remove_chatsession_current_model_and_more','2026-10-01 18:30:20.867567');
INSERT INTO "django_migrations" VALUES(35,'chat','0003_knowledgedocument_knowledgechunk','2026-10-01 18:30:20.880569');
INSERT INTO "django_migrations" VALUES(36,'core','0001_initial','2026-10-01 18:30:20.902354');
INSERT INTO "django_migrations" VALUES(37,'directory','0001_initial','2026-10-01 18:30:20.909789');
INSERT INTO "django_migrations" VALUES(38,'directory','0002_supportcontact_email','2026-10-01 18:30:20.918691');
INSERT INTO "django_migrations" VALUES(39,'directory','0003_support_contact_details','2026-10-01 18:30:20.941130');
INSERT INTO "django_migrations" VALUES(40,'milkbank','0005_milkbankrequest_response_deadline','2026-10-01 18:30:20.956319');
INSERT INTO "django_migrations" VALUES(41,'milkbank','0006_donorquestionnaire_match_mobile_form','2026-10-01 18:30:21.155793');
INSERT INTO "django_migrations" VALUES(42,'milkbank','0007_milkbankrequest_needs_representative_and_more','2026-10-01 18:30:21.210108');
INSERT INTO "django_migrations" VALUES(43,'milkbank','0008_milkbankrequest_amount_ml_and_more','2026-10-01 18:30:21.234588');
INSERT INTO "django_migrations" VALUES(44,'milkbank','0009_transactionrecord_amount_ml','2026-10-01 18:30:21.249752');
INSERT INTO "django_migrations" VALUES(45,'milkbank','0010_milkbankrequest_clinic_info_and_more','2026-10-01 18:30:21.318607');
INSERT INTO "django_migrations" VALUES(46,'milkbank','0011_fabella_current_address','2026-10-01 18:30:21.338518');
INSERT INTO "django_migrations" VALUES(47,'notifications','0001_initial','2026-10-01 18:30:21.360281');
INSERT INTO "django_migrations" VALUES(48,'sessions','0001_initial','2026-10-01 18:30:21.372469');
CREATE TABLE "django_session" ("session_key" varchar(40) NOT NULL PRIMARY KEY, "session_data" text NOT NULL, "expire_date" datetime NOT NULL);
CREATE TABLE "milkbank_donorquestionnaire" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "good_general_health" bool NOT NULL, "serology_photo" varchar(100) NULL, "submitted_at" datetime NOT NULL, "request_id" bigint NOT NULL UNIQUE REFERENCES "milkbank_milkbankrequest" ("id") DEFERRABLE INITIALLY DEFERRED, "lactating_with_excess_supply" bool NOT NULL, "free_of_infectious_disease" bool NOT NULL, "recent_transfusion_or_transplant" bool NOT NULL, "uses_tobacco_alcohol_or_drugs" bool NOT NULL, "on_medication_or_supplements" bool NOT NULL, "medication_details" varchar(255) NOT NULL, "has_recent_serology_test" bool NOT NULL);
CREATE TABLE "milkbank_facility" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "name" varchar(255) NOT NULL, "type" varchar(100) NOT NULL, "contact" varchar(50) NOT NULL, "address" varchar(500) NOT NULL, "operating_hours" varchar(100) NOT NULL, "donor_requirements" text NOT NULL, "recipient_requirements" text NOT NULL, "unavailable_donor_dates" text NOT NULL CHECK ((JSON_VALID("unavailable_donor_dates") OR "unavailable_donor_dates" IS NULL)), "unavailable_recipient_dates" text NOT NULL CHECK ((JSON_VALID("unavailable_recipient_dates") OR "unavailable_recipient_dates" IS NULL)), "is_operational" bool NOT NULL, "capacity" integer unsigned NOT NULL CHECK ("capacity" >= 0), "booked_count" integer unsigned NOT NULL CHECK ("booked_count" >= 0), "stock_level_ml" integer unsigned NOT NULL CHECK ("stock_level_ml" >= 0), "latitude" real NOT NULL, "longitude" real NOT NULL);
INSERT INTO "milkbank_facility" VALUES(1,'St. Luke''s Medical Center','Accredited Human Milk Bank','(02) 8723-0101','279 E. Rodriguez Sr. Ave, Quezon City','8:00 AM - 5:00 PM (Mon-Fri)','','','[]','[]',1,18,11,740,14.6091,121.0223);
INSERT INTO "milkbank_facility" VALUES(2,'Philippine General Hospital','Hospital Depot','(02) 8554-8400','Taft Ave, Ermita, Manila','8:00 AM - 5:00 PM (Mon-Fri)','','','[]','[]',1,36,23,1620,14.5764,120.985);
INSERT INTO "milkbank_facility" VALUES(3,'St. Martin de Porres','Hospital Depot','','','8:00 AM - 5:00 PM (Mon-Fri)','','','[]','[]',1,12,9,185,14.5794,121.0359);
INSERT INTO "milkbank_facility" VALUES(4,'Dr. Jose Fabella Memorial Hospital Human Milk Bank','Accredited Human Milk Bank','8866-7960','San Lazaro Compound, Tayuman St., Santa Cruz, Manila','8:00 AM - 5:00 PM (Mon-Fri)','','','[]','[]',1,24,7,910,14.6153,120.9804);
CREATE TABLE "milkbank_milkbankrequest" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "request_type" varchar(20) NOT NULL, "current_stage_index" integer unsigned NOT NULL CHECK ("current_stage_index" >= 0), "current_sub_status" varchar(30) NOT NULL, "staff_message" text NOT NULL, "submitted_at" datetime NOT NULL, "preferred_date" date NOT NULL, "preferred_time" varchar(20) NOT NULL, "attendance_confirmed" bool NOT NULL, "counter_offer_date" date NULL, "counter_offer_time" varchar(20) NOT NULL, "allocated_facility_id" bigint NOT NULL REFERENCES "milkbank_facility" ("id") DEFERRABLE INITIALLY DEFERRED, "owner_id" bigint NOT NULL REFERENCES "accounts_user" ("id") DEFERRABLE INITIALLY DEFERRED, "response_deadline" datetime NULL, "needs_representative" bool NOT NULL, "representative_birthday" date NULL, "representative_contact_number" varchar(50) NOT NULL, "representative_name" varchar(255) NOT NULL, "amount_ml" integer unsigned NULL CHECK ("amount_ml" >= 0), "completed_at" datetime NULL, "clinic_info" varchar(500) NOT NULL, "has_cooler" bool NOT NULL, "has_medical_abstract" bool NOT NULL, "has_prescription_proof" bool NOT NULL, "neonate_name" varchar(255) NOT NULL);
CREATE TABLE "milkbank_transactionrecord" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "type" varchar(20) NOT NULL, "facility_name" varchar(255) NOT NULL, "date" date NOT NULL, "status" varchar(20) NOT NULL, "owner_id" bigint NOT NULL REFERENCES "accounts_user" ("id") DEFERRABLE INITIALLY DEFERRED, "amount_ml" integer unsigned NULL CHECK ("amount_ml" >= 0));
CREATE TABLE "notifications_notificationitem" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "title" varchar(255) NOT NULL, "description" text NOT NULL, "category" varchar(20) NOT NULL, "created_at" datetime NOT NULL, "is_read" bool NOT NULL, "owner_id" bigint NOT NULL REFERENCES "accounts_user" ("id") DEFERRABLE INITIALLY DEFERRED);
CREATE UNIQUE INDEX "django_content_type_app_label_model_76bd3d3b_uniq" ON "django_content_type" ("app_label", "model");
CREATE UNIQUE INDEX "auth_group_permissions_group_id_permission_id_0cd325b0_uniq" ON "auth_group_permissions" ("group_id", "permission_id");
CREATE INDEX "auth_group_permissions_group_id_b120cbf9" ON "auth_group_permissions" ("group_id");
CREATE INDEX "auth_group_permissions_permission_id_84c5c92e" ON "auth_group_permissions" ("permission_id");
CREATE UNIQUE INDEX "auth_permission_content_type_id_codename_01ab375a_uniq" ON "auth_permission" ("content_type_id", "codename");
CREATE INDEX "auth_permission_content_type_id_2f476e4b" ON "auth_permission" ("content_type_id");
CREATE UNIQUE INDEX "accounts_user_groups_user_id_group_id_59c0b32f_uniq" ON "accounts_user_groups" ("user_id", "group_id");
CREATE INDEX "accounts_user_groups_user_id_52b62117" ON "accounts_user_groups" ("user_id");
CREATE INDEX "accounts_user_groups_group_id_bd11a704" ON "accounts_user_groups" ("group_id");
CREATE UNIQUE INDEX "accounts_user_user_permissions_user_id_permission_id_2ab516c2_uniq" ON "accounts_user_user_permissions" ("user_id", "permission_id");
CREATE INDEX "accounts_user_user_permissions_user_id_e4f0a161" ON "accounts_user_user_permissions" ("user_id");
CREATE INDEX "accounts_user_user_permissions_permission_id_113bb443" ON "accounts_user_user_permissions" ("permission_id");
CREATE INDEX "milkbank_transactionrecord_owner_id_d67caeea" ON "milkbank_transactionrecord" ("owner_id");
CREATE INDEX "accounts_user_facility_id_145cb9da" ON "accounts_user" ("facility_id");
CREATE INDEX "django_admin_log_content_type_id_c4bce8eb" ON "django_admin_log" ("content_type_id");
CREATE INDEX "django_admin_log_user_id_c564eba6" ON "django_admin_log" ("user_id");
CREATE INDEX "articles_articlecomment_article_id_3562752c" ON "articles_articlecomment" ("article_id");
CREATE INDEX "articles_articlecomment_author_id_a8ec0915" ON "articles_articlecomment" ("author_id");
CREATE INDEX "chat_chatmessage_session_id_4dacb902" ON "chat_chatmessage" ("session_id");
CREATE INDEX "chat_knowle_source__9cd3c0_idx" ON "chat_knowledgechunk" ("source_kind", "source_id");
CREATE INDEX "core_auditlogentry_actor_id_fca7de11" ON "core_auditlogentry" ("actor_id");
CREATE INDEX "milkbank_milkbankrequest_allocated_facility_id_a8d7caef" ON "milkbank_milkbankrequest" ("allocated_facility_id");
CREATE INDEX "milkbank_milkbankrequest_owner_id_33f076b0" ON "milkbank_milkbankrequest" ("owner_id");
CREATE INDEX "notifications_notificationitem_owner_id_b75cc032" ON "notifications_notificationitem" ("owner_id");
CREATE INDEX "django_session_expire_date_a5c62663" ON "django_session" ("expire_date");
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('django_migrations',48);
INSERT INTO "sqlite_sequence" VALUES('django_content_type',21);
INSERT INTO "sqlite_sequence" VALUES('auth_permission',84);
INSERT INTO "sqlite_sequence" VALUES('auth_group',0);
INSERT INTO "sqlite_sequence" VALUES('accounts_pendingregistration',0);
INSERT INTO "sqlite_sequence" VALUES('accounts_user',0);
INSERT INTO "sqlite_sequence" VALUES('django_admin_log',0);
INSERT INTO "sqlite_sequence" VALUES('directory_supportcontact',4);
INSERT INTO "sqlite_sequence" VALUES('milkbank_donorquestionnaire',0);
INSERT INTO "sqlite_sequence" VALUES('milkbank_milkbankrequest',0);
INSERT INTO "sqlite_sequence" VALUES('articles_article',6);
INSERT INTO "sqlite_sequence" VALUES('milkbank_facility',4);
COMMIT;
