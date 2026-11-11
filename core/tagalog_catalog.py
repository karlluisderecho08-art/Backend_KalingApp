"""
Tagalog for the server's mother-facing messages, used by core/i18n.py.

Keyed by the exact English the server sends; ``{name}`` marks a part that
changes (a facility name, a date, a stage). Each Tagalog line must keep the
same ``{name}`` slots -- core/tests_i18n.py checks that, and that every
message the mother can receive has a line here.

Style (same as the Android app): mostly Tagalog, friendly ka/mo voice;
English only for words with no common Tagalog form (app, account, email,
password, code, Kali, appointment, HIV, serology, ID, mL) and "milk bank".

Every line was drafted from the code it comes from and then checked by an
independent Filipino-language review for meaning, grammar and wording.
"""

MESSAGES = {
    "Too many incorrect passwords, so this account is locked for now. Please contact the admin for help and assistance.":
        "Masyadong maraming maling password kaya naka-lock muna ang account na ito. Makipag-ugnayan sa admin para sa tulong.",
    "Account created. Check your email for a 6-digit verification code.":
        "Nagawa na ang iyong account. Tingnan ang iyong email para sa 6-digit na code na gagamitin mo sa pagberipika.",
    "Invalid email, or this account is already verified.":
        "Mali ang email, o beripikado na ang account na ito.",
    "This code has expired. Please request a new one.":
        "Nag-expire na ang code na ito. Humiling ng bagong code.",
    "Too many incorrect attempts. Please request a new code.":
        "Masyadong maraming maling pagsubok. Humiling ng bagong code.",
    "Incorrect verification code.":
        "Mali ang code para sa pagberipika.",
    "If that email needs verifying, a new code has been sent.":
        "Kung kailangan pang beripikahin ang email na iyan, naipadala na ang bagong code.",
    "If that email has a KalingApp account, a reset code has been sent.":
        "Kung may KalingApp account ang email na iyan, naipadala na ang code para sa pag-reset.",
    "Invalid email or code.":
        "Mali ang email o code.",
    "Incorrect reset code.":
        "Mali ang code para sa pag-reset.",
    "Password must be at least 8 characters.":
        "Dapat may hindi bababa sa 8 karakter ang password.",
    "That password is incorrect.":
        "Mali ang password na iyan.",
    "An account with this email already exists.":
        "May account na gamit ang email na ito.",
    "Location cannot be stored without consent.":
        "Hindi maaaring i-save ang iyong lokasyon nang walang pahintulot mo.",
    "Your KalingApp verification code":
        "Ang iyong KalingApp code para sa pagberipika",
    "Hi {mom_name},\n\nYour KalingApp verification code is {code}.\nIt expires in {minutes} minutes.\n\nIf you didn't try to create a KalingApp account, you can safely ignore this email.":
        "Kumusta, {mom_name}!\n\nAng code mo para maberipika ang iyong KalingApp account ay {code}.\nMag-e-expire ito pagkalipas ng {minutes} minuto.\n\nKung hindi ikaw ang sumubok na gumawa ng KalingApp account, puwede mo na lang balewalain ang email na ito.",
    "Hi there,\n\nYour KalingApp verification code is {code}.\nIt expires in {minutes} minutes.\n\nIf you didn't try to create a KalingApp account, you can safely ignore this email.":
        "Kumusta!\n\nAng code mo para maberipika ang iyong KalingApp account ay {code}.\nMag-e-expire ito pagkalipas ng {minutes} minuto.\n\nKung hindi ikaw ang sumubok na gumawa ng KalingApp account, puwede mo na lang balewalain ang email na ito.",
    "Your KalingApp password reset code":
        "Ang iyong KalingApp code para sa pag-reset ng password",
    "Hi {mom_name},\n\nYour KalingApp password reset code is {code}.\nIt expires in {minutes} minutes.\n\nIf you didn't ask to reset your KalingApp password, you can safely ignore this email -- your password hasn't been changed.":
        "Kumusta, {mom_name}!\n\nAng code mo para i-reset ang iyong KalingApp password ay {code}.\nMag-e-expire ito pagkalipas ng {minutes} minuto.\n\nKung hindi ikaw ang humiling na i-reset ang iyong KalingApp password, puwede mo na lang balewalain ang email na ito -- hindi binago ang iyong password.",
    "Hi there,\n\nYour KalingApp password reset code is {code}.\nIt expires in {minutes} minutes.\n\nIf you didn't ask to reset your KalingApp password, you can safely ignore this email -- your password hasn't been changed.":
        "Kumusta!\n\nAng code mo para i-reset ang iyong KalingApp password ay {code}.\nMag-e-expire ito pagkalipas ng {minutes} minuto.\n\nKung hindi ikaw ang humiling na i-reset ang iyong KalingApp password, puwede mo na lang balewalain ang email na ito -- hindi binago ang iyong password.",
    "A new version of KalingApp is required. Please download and install it from {download_url} to continue.":
        "Kailangan mo ng bagong bersyon ng KalingApp. I-download at i-install ito mula sa {download_url} para makapagpatuloy.",
    "No active account found with the given credentials":
        "Walang aktibong account na tugma sa email at password na inilagay mo.",
    "Given token not valid for any token type":
        "Hindi na wasto ang iyong pag-log in. Mag-log in ulit.",
    "User is inactive":
        "Hindi aktibo ang iyong account.",
    "User not found":
        "Hindi mahanap ang iyong account.",
    "Token is expired":
        "Nag-expire na ang iyong pag-log in. Mag-log in ulit.",
    "Token is invalid":
        "Hindi wasto ang iyong pag-log in. Mag-log in ulit.",
    "No active account found for the given token.":
        "Walang aktibong account para sa pag-log in na ito.",
    "This field is required.":
        "Kailangang punan ito.",
    "This field may not be blank.":
        "Hindi puwedeng blangko ito.",
    "This field may not be null.":
        "Hindi puwedeng walang laman ito.",
    "Enter a valid email address.":
        "Maglagay ng wastong email address.",
    "Ensure this field has at least {min_length} characters.":
        "Dapat may hindi bababa sa {min_length} karakter ito.",
    "Ensure this field has no more than {max_length} characters.":
        "Hanggang {max_length} na karakter lang ang puwede rito.",
    "Not a valid string.":
        "Hindi wastong teksto ito.",
    "A valid integer is required.":
        "Kailangan ng wastong buong numero.",
    "Ensure this value is greater than or equal to {min_value}.":
        "Siguraduhing {min_value} o higit pa ang inilagay mo.",
    "Ensure this value is less than or equal to {max_value}.":
        "Siguraduhing {max_value} o mas mababa ang inilagay mo.",
    "A valid number is required.":
        "Maglagay ng wastong numero.",
    "Must be a valid boolean.":
        "Dapat oo o hindi ang sagot.",
    "Request was throttled. Expected available in {wait} seconds.":
        "Masyadong madalas ang pagsubok mo. Subukan ulit pagkalipas ng {wait} segundo.",
    "Request was throttled. Expected available in {wait} second.":
        "Masyadong madalas ang pagsubok mo. Subukan ulit pagkalipas ng {wait} segundo.",
    "Authentication credentials were not provided.":
        "Hindi ka pa naka-log in. Mag-log in muna para magpatuloy.",
    "Incorrect authentication credentials.":
        "Mali ang mga detalye sa pag-log in.",
    "You do not have permission to perform this action.":
        "Wala kang pahintulot na gawin ito.",
    "No NotificationItem matches the given query.":
        "Hindi nahanap ang abisong ito.",
    "Not found.":
        "Hindi ito nahanap.",
    "Method \"{method}\" not allowed.":
        "Hindi pinapayagan ang method na \"{method}\".",
    "Request was throttled.":
        "Masyadong madalas ang pagsubok mo. Maghintay sandali at subukan ulit.",
    "Location required. Call POST /auth/location/ first.":
        "Kailangan ang iyong lokasyon. Ibahagi muna ang iyong lokasyon, saka subukan ulit.",
    "No facility is available right now -- they're either closed or fully booked. Please try again later.":
        "Walang bakanteng pasilidad sa ngayon -- sarado o puno na ang lahat. Subukan ulit mamaya.",
    "Every milk bank is fully booked on {date}. Please choose another date.":
        "Puno na ang lahat ng milk bank sa {date}. Pumili ng ibang petsa.",
    "{time} on {date} is fully booked for donors. Please choose another time.":
        "Puno na ang {time} sa {date} para sa mga donor. Pumili ng ibang oras.",
    "No milk bank available right now accepts a request of {requested_ml} mL. Please check the amounts shown for your milk bank and try again.":
        "Walang bakanteng milk bank sa ngayon na tumatanggap ng kahilingang {requested_ml} mL. Tingnan ang daming nakasaad para sa iyong milk bank, saka subukan ulit.",
    "No milk bank currently holds that much milk. The most available right now is {max_available_ml} mL -- please request that amount or less.":
        "Walang milk bank na may ganoon karaming gatas sa ngayon. {max_available_ml} mL ang pinakamaraming available -- humiling ng ganitong dami o mas kaunti.",
    "You already have an open request.":
        "May bukas ka pang kahilingan.",
    # Breastmilk Collection method (donor form) and the booking chat.
    "Paste the delivery booking link so the facility can track your courier.":
        "I-paste ang link ng iyong delivery booking para masubaybayan ng pasilidad ang iyong courier.",
    "Enter a full web link that starts with https://":
        "Maglagay ng buong link na nagsisimula sa https://",
    "Choose how your milk will reach the facility.":
        "Piliin kung paano makakarating sa pasilidad ang iyong gatas.",
    "Only a donation has a collection method.":
        "Ang donasyon lang ang may paraan ng pagkolekta.",
    "This booking has already ended.":
        "Tapos na ang reserbasyong ito.",
    "Your milk has already been collected.":
        "Nakolekta na ang iyong gatas.",
    "This booking has ended, so its chat is closed.":
        "Tapos na ang reserbasyong ito, kaya sarado na ang chat nito.",
    "A message can't be empty.":
        "Hindi puwedeng walang laman ang mensahe.",
    "New message from {facility}":
        "Bagong mensahe mula sa {facility}",
    "{facility} sent you a message about your booking. Open Booking Status to reply.":
        "Nagpadala ang {facility} ng mensahe tungkol sa iyong reserbasyon. Buksan ang Booking Status para sumagot.",
    "Milk Bank Request Submitted":
        "Naipasa na ang Kahilingan sa Milk Bank",
    "Your donor request was submitted to {facility_name}.":
        "Naipasa na ang iyong kahilingang mag-donate sa {facility_name}.",
    "Your recipient request was submitted to {facility_name}.":
        "Naipasa na ang iyong kahilingan ng gatas sa {facility_name}.",
    "Cannot move from {from_status} to {to_status}":
        "Hindi maililipat mula {from_status} papuntang {to_status}",
    "The date the facility offered has filled up since. Please choose a new time instead.":
        "Napuno na ang petsang iminungkahi ng pasilidad. Pumili na lang ng bagong oras.",
    "The time the facility offered has filled up since. Please choose a new time instead.":
        "Napuno na ang oras na iminungkahi ng pasilidad. Pumili na lang ng bagong oras.",
    "{facility_name} is fully booked on {date}. Please choose another date.":
        "Puno na ang {facility_name} sa {date}. Pumili ng ibang petsa.",
    "Milk Bank Request Update":
        "Update sa Kahilingan sa Milk Bank",
    "Your request has moved to the \"{stage}\" phase.":
        "Nasa yugtong \"{stage}\" na ang iyong kahilingan.",
    "Only donor requests have a questionnaire.":
        "Ang mga kahilingang mag-donate lang ang may mga tanong sa pagsusuri.",
    "A questionnaire was already submitted for this request.":
        "Naipasa na ang mga sagot sa pagsusuri para sa kahilingang ito.",
    "Enter an amount greater than 0 mL.":
        "Maglagay ng dami na higit sa 0 mL.",
    "The most any single request can be is {ceiling_ml} mL.":
        "{ceiling_ml} mL ang pinakamarami para sa isang kahilingan.",
    "Enter a whole number of millilitres.":
        "Maglagay ng buong numero ng mL.",
    "{bottles} bottle(s) is {total_ml} mL, not {ml} mL.":
        "Ang {bottles} bote ay {total_ml} mL, hindi {ml} mL.",
    "Serology photo must be an image file.":
        "Larawan (image file) lang ang puwedeng ilakip para sa serology test.",
    "Please attach a photo of your serology test result.":
        "Ilakip ang larawan ng resulta ng iyong serology test.",
    "Milk Bank Request: Awaiting Attendance":
        "Kahilingan sa Milk Bank: Hinihintay ang Pagdalo",
    "Milk Bank Request: Scheduled":
        "Kahilingan sa Milk Bank: Nakaiskedyul",
    "Milk Bank Request: Declined":
        "Kahilingan sa Milk Bank: Tinanggihan",
    "Milk Bank Request: Expired":
        "Kahilingan sa Milk Bank: Nag-expire",
    "Milk Bank Request: Counter Offer":
        "Kahilingan sa Milk Bank: Bagong Mungkahi",
    "Milk Bank Request: Completed":
        "Kahilingan sa Milk Bank: Tapos na",
    "The facility accepted your request. Please confirm your attendance.":
        "Tinanggap ng pasilidad ang iyong kahilingan. Kumpirmahin ang iyong pagdalo.",
    "Your appointment is scheduled.":
        "Nakaiskedyul na ang iyong appointment.",
    "The facility declined your request.":
        "Tinanggihan ng pasilidad ang iyong kahilingan.",
    "Your request has expired.":
        "Nag-expire na ang iyong kahilingan.",
    "The facility proposed a new date for your appointment.":
        "Nagmungkahi ang pasilidad ng bagong petsa para sa iyong appointment.",
    "Your booking is complete. Thank you!":
        "Tapos na ang iyong reserbasyon. Salamat!",
    "The facility didn't respond to your request within 8 business hours, so it has expired. Please submit a new request.":
        "Hindi sumagot ang pasilidad sa iyong kahilingan sa loob ng 8 oras ng trabaho, kaya nag-expire na ito. Magpasa ng bagong kahilingan.",
    "Your request expired because attendance wasn't confirmed within 8 business hours of the facility's acceptance. Please submit a new request.":
        "Nag-expire ang iyong kahilingan dahil hindi nakumpirma ang pagdalo mo sa loob ng 8 oras ng trabaho mula nang tanggapin ito ng pasilidad. Magpasa ng bagong kahilingan.",
    "Not able to donate at this time":
        "Hindi ka muna makakapag-donate sa ngayon",
    "Your blood screening showed a result that means you can't donate milk right now. This rule protects the fragile babies who receive donor milk, and it doesn't always mean something is wrong with you. Please visit your doctor or health center so they can check this with you. The milk bank can also explain the result in person.":
        "Lumabas sa pagsusuri ng iyong dugo ang isang resulta na nangangahulugang hindi ka muna makakapag-donate ng gatas sa ngayon. Pinoprotektahan ng patakarang ito ang mga maselang sanggol na tumatanggap ng donasyong gatas, at hindi ito palaging nangangahulugan na may problema sa iyo. Magpatingin sa iyong doktor o health center para masuri nila ito kasama mo. Maaari ring ipaliwanag ng milk bank ang resulta nang personal.",
    "The health check during your screening found something that needs attention before you can donate. Please visit your doctor or health center so they can check this with you. The milk bank can also explain the result in person.":
        "May nakita sa pisikal na pagsusuri sa iyo na kailangan munang matugunan bago ka makapag-donate. Magpatingin sa iyong doktor o health center para masuri nila ito kasama mo. Maaari ring ipaliwanag ng milk bank ang resulta nang personal.",
    "Milk sample not accepted":
        "Hindi tinanggap ang sample ng iyong gatas",
    "Your milk sample didn't pass the safety checks needed for donation. This can happen for many reasons, including how the milk was expressed or stored, and it is not a diagnosis. If you have breast pain, redness, a fever or feel unwell, please visit your doctor or health center so they can check this with you. The milk bank can also explain the result in person.":
        "Hindi pumasa ang sample ng iyong gatas sa mga pagsusuring pangkaligtasan na kailangan para sa donasyon. Maraming puwedeng dahilan nito, kasama na kung paano piniga o inimbak ang gatas, at hindi ito isang diagnosis. Kung masakit o namumula ang iyong suso, may lagnat ka, o masama ang pakiramdam mo, magpatingin sa iyong doktor o health center para masuri nila ito kasama mo. Maaari ring ipaliwanag ng milk bank ang resulta nang personal.",
    "After your counseling session, the milk bank found that donating isn't the right fit for you at this time. This is not about anything you did wrong. Thank you for wanting to help other babies.":
        "Pagkatapos ng iyong sesyon ng pagpapayo, nakita ng milk bank na hindi pa angkop sa iyo ang pag-donate sa ngayon. Hindi ito dahil may nagawa kang mali. Salamat sa pagnanais mong matulungan ang ibang mga baby.",
    "Milk not accepted":
        "Hindi tinanggap ang gatas",
    "The milk collected didn't meet the milk bank's quality standards, so it couldn't be used for donation. You're welcome to ask the facility about donating again.":
        "Hindi umabot sa pamantayan ng kalidad ng milk bank ang nakolektang gatas, kaya hindi ito magagamit bilang donasyon. Puwede mong tanungin ang pasilidad tungkol sa muling pag-donate.",
    "A newer blood test is needed":
        "Kailangan ng mas bagong blood test",
    "Your blood test result is older than the milk bank can accept (it must be from the last 6 months). Please get a new blood test, then submit a new request.":
        "Masyadong luma na ang resulta ng iyong blood test para tanggapin ng milk bank (dapat ay mula sa nakaraang 6 na buwan). Magpa-blood test ulit, saka magpasa ng bagong kahilingan.",
    "Requirements incomplete":
        "Hindi kumpleto ang mga kailangan",
    "Some of the documents needed for your request were missing or couldn't be accepted. Please check the requirements, then submit a new request.":
        "Kulang o hindi matanggap ang ilan sa mga dokumentong kailangan para sa iyong kahilingan. Tingnan ang mga kailangan, saka magpasa ng bagong kahilingan.",
    "Not enough donor milk right now":
        "Kulang ang donasyong gatas sa ngayon",
    "The milk bank doesn't have enough donor milk at the moment to fill your request. Please try again later. We're sorry we couldn't help this time.":
        "Kulang ang donasyong gatas ng milk bank sa ngayon para matugunan ang iyong kahilingan. Subukan ulit mamaya. Pasensya na at hindi ka namin natulungan ngayon.",
    "Request not accepted":
        "Hindi tinanggap ang kahilingan",
    "The facility wasn't able to accept your request. Please contact them if you have questions.":
        "Hindi nagawang tanggapin ng pasilidad ang iyong kahilingan. Makipag-ugnayan sa kanila kung may tanong ka.",
    "Request expired":
        "Nag-expire ang kahilingan",
    "The facility didn't respond to your request in time, so it expired. Please submit a new request.":
        "Hindi nakasagot ang pasilidad sa iyong kahilingan sa takdang oras, kaya nag-expire ito. Magpasa ng bagong kahilingan.",
    "Your attendance wasn't confirmed in time after the facility accepted your request, so it expired. Please submit a new request.":
        "Hindi nakumpirma sa takdang oras ang pagdalo mo matapos tanggapin ng pasilidad ang iyong kahilingan, kaya nag-expire ito. Magpasa ng bagong kahilingan.",
    "Thank you for donating! Your milk will help feed babies who need it.":
        "Salamat sa iyong donasyon! Makakatulong ang iyong gatas sa pagpapakain ng mga baby na nangangailangan nito.",
    "Your request is complete. We hope your baby is doing well.":
        "Tapos na ang iyong kahilingan. Sana ay nasa mabuting kalagayan ang iyong baby.",
    "Completed":
        "Tapos na",
    "{title}. {message}":
        "{title}. {message}",
    "{title}. {message} Note from the facility: {facility_note}":
        "{title}. {message} Paalala mula sa pasilidad: {facility_note}",
    "\"{input}\" is not a valid choice.":
        "Hindi wastong pagpipilian ang \"{input}\".",
    "Date has wrong format. Use one of these formats instead: {format}.":
        "Mali ang pagkakasulat ng petsa. Gamitin ang isa sa mga anyong ito: {format}.",
    "The submitted data was not a file. Check the encoding type on the form.":
        "Hindi file ang ipinadalang datos. Tingnan ang encoding type ng form.",
    "The submitted file is empty.":
        "Walang laman ang file na ipinadala.",
    "Ensure this filename has at most {max_length} characters (it has {length}).":
        "Siguraduhing hindi lalampas sa {max_length} na karakter ang pangalan ng file (mayroon itong {length}).",
    "Unsupported media type \"{media_type}\" in request.":
        "Hindi suportado ang media type na \"{media_type}\" sa ipinadala.",
    "No MilkBankRequest matches the given query.":
        "Hindi mahanap ang kahilingang ito.",
    "No Facility matches the given query.":
        "Hindi mahanap ang pasilidad.",
    "No filename could be determined.":
        "Hindi matukoy ang pangalan ng file.",
    "Comment Removed":
        "Inalis ang Komento",
    "Your comment on \"{article_title}\" was removed after it was reported by {report_count} members of the community.":
        "Inalis ang komento mo sa \"{article_title}\" matapos itong iulat ng {report_count} miyembro ng komunidad.",
    "Quezon City government hospital and home of the Quezon City Human Milk Bank (2nd floor). For the milk bank, call the hospital and ask for local 209, or call 0960-363-6492. Milk bank hours: daily 8:00 AM - 5:00 PM, holidays 8:00 AM - 12:00 NN.":
        "Ospital ng pamahalaan ng Quezon City at kinaroroonan ng Quezon City Human Milk Bank (ikalawang palapag). Para sa milk bank, tumawag sa ospital at magpakonekta sa local 209, o tumawag sa 0960-363-6492. Oras ng milk bank: araw-araw, 8:00 AM - 5:00 PM; tuwing pista opisyal, 8:00 AM - 12:00 NN.",
    "Private hospital with breastfeeding support services and a human milk bank. Call the trunk line and ask for lactation support; customer service is on local 4122.":
        "Pribadong ospital na may mga serbisyong pansuporta sa pagpapasuso at may human milk bank. Tumawag sa pangunahing linya (trunk line) at magpakonekta sa lactation support; nasa local 4122 naman ang customer service.",
    "Government-accredited human milk bank.":
        "Human milk bank na accredited ng pamahalaan.",
    "Volunteer-run breastfeeding and milk-banking advocacy organization.":
        "Organisasyong pinapatakbo ng mga boluntaryo at nagsusulong ng pagpapasuso at milk banking.",
    "No Article matches the given query.":
        "Hindi mahanap ang artikulong ito.",
    "No ArticleComment matches the given query.":
        "Hindi mahanap ang komentong ito. Baka nabura na ito.",
    "Token contained no recognizable user identification":
        "Hindi ka makilala mula sa iyong sesyon. Mag-log in ulit.",
    "I'm Kali, and I'm here specifically to help with breastfeeding and lactation questions. That question is outside what I can help with -- feel free to ask me anything about latching, milk supply, storage, or donor milk instead.":
        "Ako si Kali, at nandito ako para tumulong lang sa mga tanong tungkol sa pagpapasuso at paggawa ng gatas. Hindi ko masasagot ang tanong na iyan -- pero puwede mo akong tanungin sa halip tungkol sa pagsuso ni baby, dami ng gatas, pagtatabi ng gatas, o donasyong gatas.",
    "A good latch means baby's mouth covers most of the areola, not just the nipple, with lips flanged outward. Pain that continues throughout a feed usually means the latch needs adjusting -- breaking suction gently and relatching often helps. If pain persists, an IBCLC lactation consultant can check for tongue-tie or positioning issues in person.":
        "Maayos ang pagsuso ni baby kapag natatakpan ng bibig niya ang malaking bahagi ng areola, hindi lang ang utong, at nakausli palabas ang mga labi niya. Kapag tuloy-tuloy ang sakit habang nagpapadede ka, kadalasan ay kailangang ayusin ang pagsuso -- madalas ay nakakatulong na dahan-dahang alisin ang pagkakasipsip niya at ipasuso siyang muli. Kung hindi pa rin nawawala ang sakit, matitingnan nang personal ng isang IBCLC lactation consultant kung may tongue-tie si baby o may problema sa posisyon.",
    "Milk supply works on demand: more frequent, effective removal (nursing or pumping) signals the body to make more. Common supply boosters include nursing on both sides, pumping after feeds, and staying hydrated and rested. If you're worried supply is genuinely low, a lactation consultant or your pediatric clinic can check baby's weight gain, which is the most reliable sign.":
        "Nakadepende ang dami ng gatas sa pangangailangan: kapag mas madalas at epektibong nailalabas ang gatas (sa pagpapasuso o pagpa-pump), senyales ito sa katawan mo na gumawa ng mas maraming gatas. Ilan sa mga karaniwang paraan para dumami ang gatas: ipasuso si baby sa magkabilang suso, mag-pump pagkatapos magpadede, at uminom ng sapat na tubig at magpahinga nang maayos. Kung nag-aalala ka na talagang kaunti ang gatas mo, matitingnan ng isang lactation consultant o ng pediatric clinic mo ang pagdagdag ng timbang ni baby, na siyang pinakamaaasahang palatandaan.",
    "Freshly expressed milk is generally fine at room temperature for a few hours, in the fridge for several days, and in a freezer for months -- always label with the date and use the oldest milk first. Thawed milk shouldn't be refrozen. WHO/AAP guidance has the specific time windows if you need exact numbers for your storage method.":
        "Ang bagong pigang gatas ay karaniwang ayos lang sa temperatura ng kuwarto nang ilang oras, sa refrigerator nang ilang araw, at sa freezer nang ilang buwan -- laging lagyan ito ng petsa at unahing gamitin ang pinakalumang gatas. Huwag nang i-freeze ulit ang gatas na natunaw na. Nasa gabay ng WHO/AAP ang eksaktong tagal kung kailangan mo ng tiyak na bilang para sa paraan mo ng pagtatabi.",
    "Milk donation and receiving pasteurized donor milk both go through an accredited milk bank, which screens donors and pasteurizes all stored milk for safety. You can start either the donor or recipient pathway from the Milk Bank section of this app.":
        "Dumadaan sa isang accredited na milk bank ang pag-donate ng gatas at ang pagtanggap ng pasteurized na donasyong gatas. Sinusuri ng milk bank ang mga donor at pina-pasteurize nito ang lahat ng nakatabing gatas para manatili itong ligtas. Puwede kang magsimulang mag-donate o humiling ng gatas sa bahaging Milk Bank ng app na ito.",
    "Hi, I'm Kali! I'm here to help with breastfeeding and lactation questions -- latching, milk supply, storage, or donor milk. What's on your mind?":
        "Kumusta, ako si Kali! Nandito ako para tumulong sa mga tanong mo tungkol sa pagpapasuso at paggawa ng gatas -- sa pagsuso ni baby, dami ng gatas, pagtatabi ng gatas, o donasyong gatas. Ano ang gusto mong itanong?",
    "I'm currently running on offline mode and can't reach my full knowledge base, but I'm still here to help with breastfeeding basics -- latching, milk supply, storage, or donation. Could you tell me a bit more about what you're looking for?":
        "Nasa offline mode ako ngayon kaya hindi ko ma-access ang buo kong sanggunian, pero nandito pa rin ako para tumulong sa mga pangunahing kaalaman sa pagpapasuso -- pagsuso ni baby, dami ng gatas, pagtatabi ng gatas, o pag-donate. Puwede mo bang ikuwento nang mas detalyado kung ano ang hinahanap mo?",
    "Authorization header must contain two space-delimited values":
        "Dapat may dalawang bahagi na pinaghihiwalay ng espasyo ang Authorization header.",
    "Invalid data. Expected a dictionary, but got {datatype}.":
        "Hindi wasto ang datos. Dictionary ang inaasahan, pero {datatype} ang natanggap.",
    "Milk Bank Request: {status}":
        "Kahilingan sa Milk Bank: {status}",
}

# Values that are inserted into a message's {slot}: translated there, but
# never on their own in the data the app compares (stages, statuses).
LABELS = {
    "Status": "Katayuan",
    "Booking Confirmation": "Kumpirmasyon ng Reserbasyon",
    "Counseling and Testing": "Pagpapayo at Pagsusuri",
    "Breastmilk Collection": "Pagkolekta ng Gatas",
    "Results": "Resulta",
    "Requirements": "Mga Kailangan",
    "Pending": "Naghihintay",
    "Awaiting Attendance": "Hinihintay ang Pagdalo",
    "Scheduled": "Nakaiskedyul",
    "Declined": "Tinanggihan",
    "Expired": "Nag-expire",
    "Counter Offer": "Bagong Mungkahi",
    "Counter-Offer": "Bagong Mungkahi",
    "Completed": "Tapos na",
    "pending": "Naghihintay",
    "awaiting_attendance": "Hinihintay ang Pagdalo",
    "scheduled": "Nakaiskedyul",
    "declined": "Tinanggihan",
    "expired": "Nag-expire",
    "counter_offered": "Bagong Mungkahi",
    "completed": "Tapos na",
}
