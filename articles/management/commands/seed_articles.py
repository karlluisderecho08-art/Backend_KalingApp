from django.core.management.base import BaseCommand

from articles.models import Article


class Command(BaseCommand):
    """
    manage.py seed_articles

    Seeds the Knowledge Hub with real, sourced articles the user supplied
    on 2026-08-30 -- adapted from UNICEF, WHO, and FAO material, not
    placeholder text. Each one keeps its real "Source:" attribution line
    at the end of `content`, matching the app's "Organization-Verified"
    framing -- that line is the actual evidence, not decoration.

    teaser/read_time aren't in the source documents (the originals only
    had title/category/author/date) -- those two fields are written/
    estimated here, everything else is the user's real content verbatim.
    """

    help = "Seed real Knowledge Hub articles (UNICEF/WHO/FAO-sourced)"

    def handle(self, *args, **options):
        articles = [
            {
                "title": "How to Breastfeed Correctly: Positioning and Latch",
                "category": Article.Category.LATCHING,
                "read_time": "2 min read",
                "teaser": "The fundamentals of a comfortable, effective latch -- how to position your baby and recognize the signs that feeding is going well.",
                "author": "Adapted from UNICEF (unicef.org)",
                "date": "August 2025",
                "content": """Good breastfeeding starts with the right position and a good latch. Getting these two things right helps the baby feed effectively and keeps the mother comfortable.

Positioning

Find a position that feels comfortable, whether you are sitting or lying down. Staying relaxed matters, because tension can temporarily lower milk flow. Hold the baby so that their ear, shoulder, and hip form a straight line, fully supported by your hands. The baby's mouth should face the breast directly, with the nose in line with the nipple. Keep the baby close, tummy touching your body.

Latching

Guide the baby gently toward the breast and let their natural rooting reflex take over. You can tell the latch is good when the baby's mouth is wide open, the lower lip is turned outward, and the chin touches or nearly touches the breast. Most of the areola should be inside the baby's mouth, with more of it visible above the top lip than below the bottom lip.

Effective sucking

A well-latched baby sucks slowly and deeply, pausing now and then, with occasional swallowing. Staying calm and relaxed during the feed supports bonding and helps milk production.

General tips

Feed the baby whenever they are hungry, day and night. Night feeds are especially important because they stimulate milk supply. Frequent feeding on demand naturally increases the amount of milk your body makes as it responds to the baby's needs. If a baby is too weak to suckle, expressing milk and feeding it to them is a safe alternative.

Source: UNICEF India, "How to Breastfeed Correctly." https://www.unicef.org/india/stories/how-breastfeed-correctly""",
            },
            {
                "title": "5 Common Breastfeeding Problems and How to Handle Them",
                "category": Article.Category.LATCHING,
                "read_time": "3 min read",
                "teaser": "From latch trouble to low supply, engorgement, cracked nipples, and blocked ducts -- practical guidance for the most common breastfeeding challenges.",
                "author": "Adapted from UNICEF (unicef.org)",
                "date": "2026",
                "content": """Breastfeeding is not always easy. It takes time and practice for both mother and baby, and many mothers run into challenges along the way. The good news is that most problems can be managed with the right support. If you are struggling, reach out to a midwife, lactation specialist, or health care provider.

1. Getting a good latch

A proper latch is the foundation of comfortable, effective feeding. Position the baby's nose opposite your nipple and let their head tilt back slightly so the top lip brushes the nipple, which prompts them to open wide. When the mouth is open, bring the baby quickly to the breast, chin leading, so they take a large mouthful of breast rather than just the nipple. Signs of a good latch: feeding feels comfortable with no pain, more areola shows above the mouth than below, the mouth is wide open, the lower lip turns outward, and the chin touches or nearly touches the breast.

2. Low milk supply

Worrying about producing enough is common, and there is usually an identifiable cause a provider can help you find. Frequent contributors include a delayed start to breastfeeding, little or no skin-to-skin contact, poor attachment, feeding on a fixed schedule, short or infrequent feeds, no nighttime feeding, and stress or fatigue. To protect your supply, begin skin-to-skin contact and breastfeeding as soon as possible after delivery, seek skilled support to confirm good attachment, room-in with your baby around the clock, and breastfeed exclusively unless a provider advises otherwise.

3. Engorged breasts

Full breasts feel hot, heavy, and hard, but milk still flows and there is no fever. Engorgement is different and more serious: the breast becomes painfully swollen, tight, shiny, and possibly red, milk stops flowing, and a fever may last around 24 hours. Engorgement is usually caused by too much milk sitting in the breast, a delayed start to feeding, poor attachment, or infrequent milk removal. Prevent it by starting breastfeeding soon after delivery, ensuring a good latch, feeding without restriction, and removing milk frequently by feeding, hand expression, or pumping. See a provider if engorgement sets in.

4. Cracked nipples

Sore or cracked nipples are usually a sign the baby is not attached properly, so the first fix is correcting the latch. If a nipple cracks or bleeds, contact your provider promptly. At home, dabbing a little expressed breast milk onto the nipple after feeds can soothe soreness, since breast milk has natural healing properties.

5. Blocked ducts and mastitis

Milk reaches the baby through a system of ducts, and sometimes part of that system becomes blocked, which is painful and stops milk from flowing freely. If a blocked duct does not clear, or milk builds up, the breast can become inflamed, a condition called mastitis. Causes include short or infrequent feeds, incomplete milk removal, damaged tissue, or bacteria entering through a cracked nipple. To relieve symptoms, improve milk removal, correct the latch, feed more often, massage gently toward the nipple, apply a warm compress, wear loose clothing, and vary feeding positions. See a provider if pain persists.

Breastfeeding is not a one-woman job. Bumps along the way are normal, so bring any concerns to your health care provider and lean on family and friends for support.

Source: UNICEF Kosovo Programme, "5 common breastfeeding problems." https://www.unicef.org/kosovoprogramme/5-common-breastfeeding-problems""",
            },
            {
                "title": "Breastfeeding When You Return to Work",
                "category": Article.Category.STORAGE,
                "read_time": "4 min read",
                "teaser": "Practical tips for continuing to breastfeed after returning to work, from planning ahead to safely storing expressed milk.",
                "author": "Adapted from UNICEF (unicef.org)",
                "date": "2026",
                "content": """Balancing work and family is hard at the best of times, and for breastfeeding mothers returning to work after giving birth it can feel overwhelming. Breastfeeding gives children the healthiest start in life: breast milk acts as a baby's first vaccine, supports brain development, and protects the mother's health. For this reason, UNICEF and WHO urge governments and employers to adopt family-friendly policies that give mothers the time, space, and support they need to keep breastfeeding. The tips below can make the transition back to work a little easier.

Plan ahead

Let your family, friends, and co-workers know about your decision to breastfeed, and look for breastfeeding support groups who can help you find solutions along the way. Mothers continue breastfeeding while working in several ways: keeping the baby with them and feeding throughout the day; going home to breastfeed, or having someone bring the baby to them, if work is nearby; using a nearby day care where they can feed during the workday; or, when feeding during work hours is not possible, learning to express milk and leaving a supply for a caregiver.

Breastfeeding support at work

Find out what your employer offers, such as on-site childcare or the option to bring your baby to work. At a minimum, employers must comply with existing laws on maternity leave and workplace breastfeeding support, so check what is required where you live. Ask about your workplace lactation policy and whether there is a lactation room and how it works. If there is none, talk to your manager or HR about arranging a private space, and consider sharing UNICEF's recommendations for employers.

Rather than returning straight to a full five-day week, ask whether your schedule can be flexible at first. A gradual return, temporary part-time work, or teleworking can all ease the disruption to your breastfeeding routine. If you work nights, ask whether you can be reassigned to a morning shift so you can breastfeed directly at night. Remember that your employer is responsible for your safety at work and must not discriminate on the basis of pregnancy, breastfeeding, or family status.

What a lactation room should look like

A lactation or breastfeeding room should be a clean, comfortable, safe, and private space where mothers can breastfeed, express milk, and store it properly. Ideally it includes a cold storage system, preferably a fridge or freezer for the room's exclusive use, along with handwashing facilities and supplies such as drinking water, liquid soap, hand sanitizer, surface cleaner, and paper towels. It should have a comfortable, individual chair with adjustable height and good back support, made of a material that is easy to wash and disinfect, rather than a sofa.

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

Source: UNICEF Parenting, "Breastfeeding when you return to work." https://www.unicef.org/parenting/food-nutrition/breastfeeding-workplace""",
            },
            {
                "title": "Nutrition During Pregnancy and Breastfeeding",
                "category": Article.Category.NUTRITION,
                "read_time": "3 min read",
                "teaser": "What to eat before, during, and after pregnancy to stay healthy and support your baby's growth, from folate-rich foods to daily food group guidelines.",
                "author": "Adapted from the Food and Agriculture Organization of the United Nations (FAO)",
                "date": "2026",
                "content": """Good eating habits before and during pregnancy help keep a mother healthy and allow her baby to grow and develop properly. A woman's need for energy and most nutrients rises during pregnancy and breastfeeding, so a healthy, balanced diet that meets those needs is important throughout this time.

Before pregnancy

A woman's health before pregnancy affects both her ability to conceive and the health of her future baby, so being in good health at a healthy body weight matters. Being too thin or too heavy raises the risk of complications. Folate is especially important in the early stages of pregnancy, because too little of it can lead to serious birth defects. Women who could become pregnant are encouraged to eat plenty of folate-rich foods each day, particularly leafy green vegetables, beans, peas and other legumes, and liver. If diet alone cannot meet folate needs, folic-acid-fortified foods or supplements may help. Always consult a doctor or health professional before taking any vitamin or mineral supplements.

During pregnancy

Every pregnant woman needs a good, balanced diet and should gain weight to support a healthy pregnancy and delivery. It is not necessary to "eat for two"; a woman starting pregnancy at a healthy weight generally needs only about 280 extra calories a day. During pregnancy the mother's own nutrient stores can run low, which raises her risk of illness, and a baby who does not get enough nutrition before birth is more likely to have health and development problems later.

Several nutrients matter especially: protein, to build new tissue, blood, cells, and bone; iron, whose needs are high and often require supplements; iodine, which helps prevent serious birth defects affecting the brain; folate, to prevent birth defects in the first weeks; and zinc and vitamins A and C. A helpful daily pattern is around four glasses of milk or milk products, three portions of meat, fish, eggs, or beans, four portions of fruit and vegetables, six portions of bread and cereals, and plenty of fluids. Pregnant women should avoid alcohol and have regular medical check-ups.

During breastfeeding

Breastfeeding also demands extra energy and nutrients, because the mother must replace what she passes to her baby through her milk. If her diet does not meet these needs, her baby will draw on her own stores, putting her health at risk and possibly affecting the baby's development. The important nutrients are the same as in pregnancy: protein, zinc, calcium, vitamins A and C, iron, and folate. Extra servings of milk and high-protein snacks between meals, or one additional small meal a day, are good ways to meet these needs. A breastfeeding mother needs a varied, nutritious diet built on staple foods, vegetables, legumes, meat and fish, and plenty of fruit, along with plenty of water, milk, and other fluids. Because it can take two to three years after breastfeeding ends for a mother's nutrient stores to fully recover, spacing pregnancies well apart supports the health of both the mother and her future babies.

Weight gain during pregnancy

All pregnant women need to gain weight, regardless of their weight before pregnancy, to support the growing baby and the added growth of the uterus, breasts, blood, and other tissues. As a general guide: a woman at a healthy weight should gain roughly 11.5 to 16 kg; an underweight woman, about 12.5 to 18 kg; an overweight woman, about 7 to 11.5 kg; and an obese woman, about 5 to 9 kg. A health professional can give guidance suited to each woman's situation.

Source: Food and Agriculture Organization of the United Nations (FAO), "Nutrition during pregnancy and breastfeeding," from the FAO nutrition education fact sheets. https://www.fao.org/4/i3261e/i3261e08.pdf""",
            },
            {
                "title": "Unang Yakap (First Embrace): Encouraging Breastfeeding from the Start",
                "category": Article.Category.NEWBORN,
                "read_time": "2 min read",
                "teaser": "The Philippine DOH's four-step protocol for the first moments after birth, designed to give breastfeeding the strongest possible start.",
                "author": "Adapted from World Health Organization (who.int)",
                "date": "2026",
                "content": """Unang Yakap, or "First Embrace," is a campaign of the Philippines' Department of Health (DOH), in cooperation with the World Health Organization (WHO), to put Essential Intrapartum Newborn Care (EINC) into practice. EINC is a set of simple, evidence-based steps performed right after birth that give both mother and newborn the best possible start. Public and private hospitals in the Philippines have been directed to follow the Unang Yakap protocol since 2009.

The four core steps

EINC follows four time-bound steps immediately after delivery:

1. Immediate and thorough drying of the baby.
2. Early skin-to-skin contact between the mother and the newborn.
3. Properly timed clamping of the umbilical cord.
4. Non-separation of mother and baby, so that breastfeeding can begin early.

Why early breastfeeding matters

The main goal of Unang Yakap is to support breastfeeding from the very first moments of life. When the baby is placed directly on the mother's breast, skin to skin, the baby becomes familiar with the breast as the source of food. Rather than forcing the baby to latch, caregivers watch for the baby's own feeding cues, such as tonguing, licking, and opening of the mouth, and gently guide the baby to the breast when those cues appear. This respects the baby's natural readiness to feed.

Breastfeeding is encouraged within the first 60 to 90 minutes after birth. This early window matters because it is when colostrum, the protein- and nutrient-rich first milk, is released, giving the newborn important protection against infection.

To protect breastfeeding success, formula milk should not be given, and teats or pacifiers should be avoided to prevent nipple confusion. The mother's breast should be the baby's first and only source of food.

For whom

Unang Yakap is routine for low-risk births, but babies of higher-risk pregnancies can benefit from it just as much. It is intended for all births unless a specific circumstance prevents the full four steps, such as a baby who is not breathing even after immediate and thorough drying.

Source: World Health Organization Philippines, "Unang Yakap: Encouraging Breastfeeding from the Start." https://www.who.int/philippines/news/feature-stories/detail/unang-yakap-encouraging-breastfeeding-from-the-start""",
            },
            {
                "title": "Why Breastfeeding Matters for Your Newborn",
                "category": Article.Category.NEWBORN,
                "read_time": "3 min read",
                "teaser": "How breast milk protects your baby's health in the first six months, and what your options are if breastfeeding isn't possible.",
                "author": "Adapted from UNICEF (unicef.org)",
                "date": "May 2026",
                "content": """Breast milk gives a newborn everything they need for the first six months of life. Exclusive breastfeeding means feeding a baby only breast milk during this period, with no other food or drink, not even water. It is safe, clean, and always ready, even in places where clean water is hard to find.

Beginning breastfeeding right after birth, with skin-to-skin contact, helps keep the baby warm, strengthens their immune system, builds the bond between mother and child, and supports the mother's milk supply over time. The very first milk, called colostrum, is rich in antibodies that protect the newborn against illness and infection. Because of this, breast milk works almost like a personalized medicine that adjusts to each baby's needs.

Breastfed babies tend to have fewer ear infections, less diarrhea, and a lower chance of pneumonia and other common childhood illnesses. When a mother catches an illness, her body produces antibodies in her milk that help shield the baby from the same infection. Breastfeeding is also affordable for families and gentler on the environment than formula, which requires manufacturing, packaging, and transport.

Many mothers worry in the first few days that they are not producing enough milk. In most cases, the small amount of colostrum is exactly right for a newborn's tiny stomach. No water, juice, or formula is needed at this stage. The most reliable way to establish breastfeeding is early and frequent contact: placing the baby at the breast soon after birth, when the instinct to suckle is strongest, ideally with support from a health worker, midwife, or lactation counselor.

Introducing a bottle or formula too early can interfere with the baby's natural sucking reflex and is a common reason breastfeeding does not take hold. When a mother is truly unable to breastfeed, the best alternatives depend on her situation and may include her own expressed milk, screened milk from a healthy donor or a human-milk bank, or a breast-milk substitute given by cup rather than bottle.

Mothers should never be shamed or made to feel guilty if they cannot breastfeed, whether by choice or necessity. What matters most is that every baby is fed safely and that every mother has access to accurate information and skilled, non-judgmental support.

Source: UNICEF, "Why breastfeeding is critical for babies." https://www.unicef.org/stories/why-breastfeeding-best-babies""",
            },
        ]

        for data in articles:
            obj, created = Article.objects.get_or_create(title=data["title"], defaults=data)
            verb = "Created" if created else "Already exists"
            self.stdout.write(self.style.SUCCESS(f"{verb}: {obj.title}"))
