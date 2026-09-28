--
-- KalingApp backend -- schema-only dump, generated directly from the
-- live Render Postgres database (kalingapp_db) via pg_catalog/
-- information_schema queries (pg_dump's own client binary couldn't be
-- installed in this environment due to a blocked download, and the
-- one available client version didn't match the server's major
-- version anyway -- this reproduces the same DDL pg_dump would, using
-- the same pg_get_constraintdef()/pg_get_indexdef() functions it uses
-- internally).
--
-- NOTE: the accounts_user.total_drawn_ml column and its CHECK constraint
-- were hand-applied here to match migration accounts/0009_user_total_drawn_ml,
-- which landed after this dump was taken on 2026-09-18. Render runs
-- `manage.py migrate` on every deploy (build.sh), so the live database
-- does have them. Everything else is as dumped. Regenerate against
-- Postgres when credentials are available to remove this caveat.
--

-- ======================================================================
-- TABLE: accounts_pendingregistration
-- ======================================================================
CREATE TABLE accounts_pendingregistration (
    id bigint NOT NULL,
    email varchar(254) NOT NULL,
    password varchar(128) NOT NULL,
    mom_name varchar(150) NOT NULL,
    baby_name varchar(150) NOT NULL,
    code varchar(6) NOT NULL,
    sent_at timestamp with time zone,
    attempts smallint NOT NULL,
    created_at timestamp with time zone NOT NULL
);

ALTER TABLE accounts_pendingregistration ADD CONSTRAINT accounts_pendingregistration_pkey PRIMARY KEY (id);  -- PRIMARY KEY
ALTER TABLE accounts_pendingregistration ADD CONSTRAINT accounts_pendingregistration_email_key UNIQUE (email);  -- UNIQUE
ALTER TABLE accounts_pendingregistration ADD CONSTRAINT accounts_pendingregistration_attempts_check CHECK (attempts >= 0);  -- CHECK

CREATE INDEX accounts_pendingregistration_email_19fe7124_like ON public.accounts_pendingregistration USING btree (email varchar_pattern_ops);


-- ======================================================================
-- TABLE: accounts_user
-- ======================================================================
CREATE TABLE accounts_user (
    id bigint NOT NULL,
    password varchar(128) NOT NULL,
    last_login timestamp with time zone,
    is_superuser boolean NOT NULL,
    first_name varchar(150) NOT NULL,
    last_name varchar(150) NOT NULL,
    is_staff boolean NOT NULL,
    is_active boolean NOT NULL,
    date_joined timestamp with time zone NOT NULL,
    email varchar(254) NOT NULL,
    role varchar(20) NOT NULL,
    mom_name varchar(150) NOT NULL,
    baby_name varchar(150) NOT NULL,
    baby_age_weeks integer,
    breastfeeding_status varchar(255) NOT NULL,
    baby_birth_date date,
    pediatric_clinic varchar(255) NOT NULL,
    tracking_streaks integer NOT NULL,
    total_drawn_ml integer NOT NULL,
    latitude double precision,
    longitude double precision,
    location_consent_given boolean NOT NULL,
    location_consent_at timestamp with time zone,
    last_active_date date,
    has_seen_walkthrough boolean NOT NULL,
    email_verified boolean NOT NULL,
    facility_id bigint,
    password_reset_attempts smallint NOT NULL,
    password_reset_code varchar(6) NOT NULL,
    password_reset_sent_at timestamp with time zone
);

ALTER TABLE accounts_user ADD CONSTRAINT accounts_user_pkey PRIMARY KEY (id);  -- PRIMARY KEY
ALTER TABLE accounts_user ADD CONSTRAINT accounts_user_email_key UNIQUE (email);  -- UNIQUE
ALTER TABLE accounts_user ADD CONSTRAINT accounts_user_facility_id_145cb9da_fk_milkbank_facility_id FOREIGN KEY (facility_id) REFERENCES milkbank_facility(id) DEFERRABLE INITIALLY DEFERRED;  -- FOREIGN KEY
ALTER TABLE accounts_user ADD CONSTRAINT accounts_user_baby_age_weeks_check CHECK (baby_age_weeks >= 0);  -- CHECK
ALTER TABLE accounts_user ADD CONSTRAINT accounts_user_password_reset_attempts_check CHECK (password_reset_attempts >= 0);  -- CHECK
ALTER TABLE accounts_user ADD CONSTRAINT accounts_user_total_drawn_ml_check CHECK (total_drawn_ml >= 0);  -- CHECK
ALTER TABLE accounts_user ADD CONSTRAINT accounts_user_tracking_streaks_check CHECK (tracking_streaks >= 0);  -- CHECK

CREATE INDEX accounts_user_email_b2644a56_like ON public.accounts_user USING btree (email varchar_pattern_ops);
CREATE INDEX accounts_user_facility_id_145cb9da ON public.accounts_user USING btree (facility_id);


-- ======================================================================
-- TABLE: accounts_user_groups
-- ======================================================================
CREATE TABLE accounts_user_groups (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    group_id integer NOT NULL
);

ALTER TABLE accounts_user_groups ADD CONSTRAINT accounts_user_groups_pkey PRIMARY KEY (id);  -- PRIMARY KEY
ALTER TABLE accounts_user_groups ADD CONSTRAINT accounts_user_groups_user_id_group_id_59c0b32f_uniq UNIQUE (user_id, group_id);  -- UNIQUE
ALTER TABLE accounts_user_groups ADD CONSTRAINT accounts_user_groups_group_id_bd11a704_fk_auth_group_id FOREIGN KEY (group_id) REFERENCES auth_group(id) DEFERRABLE INITIALLY DEFERRED;  -- FOREIGN KEY
ALTER TABLE accounts_user_groups ADD CONSTRAINT accounts_user_groups_user_id_52b62117_fk_accounts_user_id FOREIGN KEY (user_id) REFERENCES accounts_user(id) DEFERRABLE INITIALLY DEFERRED;  -- FOREIGN KEY

CREATE INDEX accounts_user_groups_group_id_bd11a704 ON public.accounts_user_groups USING btree (group_id);
CREATE INDEX accounts_user_groups_user_id_52b62117 ON public.accounts_user_groups USING btree (user_id);


-- ======================================================================
-- TABLE: accounts_user_user_permissions
-- ======================================================================
CREATE TABLE accounts_user_user_permissions (
    id bigint NOT NULL,
    user_id bigint NOT NULL,
    permission_id integer NOT NULL
);

ALTER TABLE accounts_user_user_permissions ADD CONSTRAINT accounts_user_user_permissions_pkey PRIMARY KEY (id);  -- PRIMARY KEY
ALTER TABLE accounts_user_user_permissions ADD CONSTRAINT accounts_user_user_permi_user_id_permission_id_2ab516c2_uniq UNIQUE (user_id, permission_id);  -- UNIQUE
ALTER TABLE accounts_user_user_permissions ADD CONSTRAINT accounts_user_user_p_permission_id_113bb443_fk_auth_perm FOREIGN KEY (permission_id) REFERENCES auth_permission(id) DEFERRABLE INITIALLY DEFERRED;  -- FOREIGN KEY
ALTER TABLE accounts_user_user_permissions ADD CONSTRAINT accounts_user_user_p_user_id_e4f0a161_fk_accounts_ FOREIGN KEY (user_id) REFERENCES accounts_user(id) DEFERRABLE INITIALLY DEFERRED;  -- FOREIGN KEY

CREATE INDEX accounts_user_user_permissions_permission_id_113bb443 ON public.accounts_user_user_permissions USING btree (permission_id);
CREATE INDEX accounts_user_user_permissions_user_id_e4f0a161 ON public.accounts_user_user_permissions USING btree (user_id);


-- ======================================================================
-- TABLE: articles_article
-- ======================================================================
CREATE TABLE articles_article (
    id bigint NOT NULL,
    title varchar(255) NOT NULL,
    category varchar(50) NOT NULL,
    read_time varchar(50) NOT NULL,
    teaser varchar(500) NOT NULL,
    content text NOT NULL,
    author varchar(150) NOT NULL,
    rating varchar(20) NOT NULL,
    evidence_label varchar(255) NOT NULL,
    date varchar(50) NOT NULL
);

ALTER TABLE articles_article ADD CONSTRAINT articles_article_pkey PRIMARY KEY (id);  -- PRIMARY KEY


-- ======================================================================
-- TABLE: articles_articlecomment
-- ======================================================================
CREATE TABLE articles_articlecomment (
    id bigint NOT NULL,
    text text NOT NULL,
    created_at timestamp with time zone NOT NULL,
    is_reported boolean NOT NULL,
    report_reason varchar(20) NOT NULL,
    article_id bigint NOT NULL,
    author_id bigint NOT NULL
);

ALTER TABLE articles_articlecomment ADD CONSTRAINT articles_articlecomment_pkey PRIMARY KEY (id);  -- PRIMARY KEY
ALTER TABLE articles_articlecomment ADD CONSTRAINT articles_articlecomm_article_id_3562752c_fk_articles_ FOREIGN KEY (article_id) REFERENCES articles_article(id) DEFERRABLE INITIALLY DEFERRED;  -- FOREIGN KEY
ALTER TABLE articles_articlecomment ADD CONSTRAINT articles_articlecomment_author_id_a8ec0915_fk_accounts_user_id FOREIGN KEY (author_id) REFERENCES accounts_user(id) DEFERRABLE INITIALLY DEFERRED;  -- FOREIGN KEY

CREATE INDEX articles_articlecomment_article_id_3562752c ON public.articles_articlecomment USING btree (article_id);
CREATE INDEX articles_articlecomment_author_id_a8ec0915 ON public.articles_articlecomment USING btree (author_id);


-- ======================================================================
-- TABLE: articles_resourcelink
-- ======================================================================
CREATE TABLE articles_resourcelink (
    id bigint NOT NULL,
    title varchar(255) NOT NULL,
    description varchar(500) NOT NULL,
    url varchar(200) NOT NULL,
    type varchar(20) NOT NULL
);

ALTER TABLE articles_resourcelink ADD CONSTRAINT articles_resourcelink_pkey PRIMARY KEY (id);  -- PRIMARY KEY


-- ======================================================================
-- TABLE: auth_group
-- ======================================================================
CREATE TABLE auth_group (
    id integer NOT NULL,
    name varchar(150) NOT NULL
);

ALTER TABLE auth_group ADD CONSTRAINT auth_group_pkey PRIMARY KEY (id);  -- PRIMARY KEY
ALTER TABLE auth_group ADD CONSTRAINT auth_group_name_key UNIQUE (name);  -- UNIQUE

CREATE INDEX auth_group_name_a6ea08ec_like ON public.auth_group USING btree (name varchar_pattern_ops);


-- ======================================================================
-- TABLE: auth_group_permissions
-- ======================================================================
CREATE TABLE auth_group_permissions (
    id bigint NOT NULL,
    group_id integer NOT NULL,
    permission_id integer NOT NULL
);

ALTER TABLE auth_group_permissions ADD CONSTRAINT auth_group_permissions_pkey PRIMARY KEY (id);  -- PRIMARY KEY
ALTER TABLE auth_group_permissions ADD CONSTRAINT auth_group_permissions_group_id_permission_id_0cd325b0_uniq UNIQUE (group_id, permission_id);  -- UNIQUE
ALTER TABLE auth_group_permissions ADD CONSTRAINT auth_group_permissio_permission_id_84c5c92e_fk_auth_perm FOREIGN KEY (permission_id) REFERENCES auth_permission(id) DEFERRABLE INITIALLY DEFERRED;  -- FOREIGN KEY
ALTER TABLE auth_group_permissions ADD CONSTRAINT auth_group_permissions_group_id_b120cbf9_fk_auth_group_id FOREIGN KEY (group_id) REFERENCES auth_group(id) DEFERRABLE INITIALLY DEFERRED;  -- FOREIGN KEY

CREATE INDEX auth_group_permissions_group_id_b120cbf9 ON public.auth_group_permissions USING btree (group_id);
CREATE INDEX auth_group_permissions_permission_id_84c5c92e ON public.auth_group_permissions USING btree (permission_id);


-- ======================================================================
-- TABLE: auth_permission
-- ======================================================================
CREATE TABLE auth_permission (
    id integer NOT NULL,
    name varchar(255) NOT NULL,
    content_type_id integer NOT NULL,
    codename varchar(100) NOT NULL
);

ALTER TABLE auth_permission ADD CONSTRAINT auth_permission_pkey PRIMARY KEY (id);  -- PRIMARY KEY
ALTER TABLE auth_permission ADD CONSTRAINT auth_permission_content_type_id_codename_01ab375a_uniq UNIQUE (content_type_id, codename);  -- UNIQUE
ALTER TABLE auth_permission ADD CONSTRAINT auth_permission_content_type_id_2f476e4b_fk_django_co FOREIGN KEY (content_type_id) REFERENCES django_content_type(id) DEFERRABLE INITIALLY DEFERRED;  -- FOREIGN KEY

CREATE INDEX auth_permission_content_type_id_2f476e4b ON public.auth_permission USING btree (content_type_id);


-- ======================================================================
-- TABLE: chat_chatmessage
-- ======================================================================
CREATE TABLE chat_chatmessage (
    id bigint NOT NULL,
    text text NOT NULL,
    is_user boolean NOT NULL,
    is_system_notice boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    session_id bigint NOT NULL
);

ALTER TABLE chat_chatmessage ADD CONSTRAINT chat_chatmessage_pkey PRIMARY KEY (id);  -- PRIMARY KEY
ALTER TABLE chat_chatmessage ADD CONSTRAINT chat_chatmessage_session_id_4dacb902_fk_chat_chatsession_id FOREIGN KEY (session_id) REFERENCES chat_chatsession(id) DEFERRABLE INITIALLY DEFERRED;  -- FOREIGN KEY

CREATE INDEX chat_chatmessage_session_id_4dacb902 ON public.chat_chatmessage USING btree (session_id);


-- ======================================================================
-- TABLE: chat_chatsession
-- ======================================================================
CREATE TABLE chat_chatsession (
    id bigint NOT NULL,
    prompt_count integer NOT NULL,
    token_count integer NOT NULL,
    owner_id bigint NOT NULL
);

ALTER TABLE chat_chatsession ADD CONSTRAINT chat_chatsession_pkey PRIMARY KEY (id);  -- PRIMARY KEY
ALTER TABLE chat_chatsession ADD CONSTRAINT chat_chatsession_owner_id_key UNIQUE (owner_id);  -- UNIQUE
ALTER TABLE chat_chatsession ADD CONSTRAINT chat_chatsession_owner_id_aec267f8_fk_accounts_user_id FOREIGN KEY (owner_id) REFERENCES accounts_user(id) DEFERRABLE INITIALLY DEFERRED;  -- FOREIGN KEY
ALTER TABLE chat_chatsession ADD CONSTRAINT chat_chatsession_prompt_count_check CHECK (prompt_count >= 0);  -- CHECK
ALTER TABLE chat_chatsession ADD CONSTRAINT chat_chatsession_token_count_check CHECK (token_count >= 0);  -- CHECK


-- ======================================================================
-- TABLE: core_auditlogentry
-- ======================================================================
CREATE TABLE core_auditlogentry (
    id bigint NOT NULL,
    action varchar(100) NOT NULL,
    target varchar(255) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    actor_id bigint
);

ALTER TABLE core_auditlogentry ADD CONSTRAINT core_auditlogentry_pkey PRIMARY KEY (id);  -- PRIMARY KEY
ALTER TABLE core_auditlogentry ADD CONSTRAINT core_auditlogentry_actor_id_fca7de11_fk_accounts_user_id FOREIGN KEY (actor_id) REFERENCES accounts_user(id) DEFERRABLE INITIALLY DEFERRED;  -- FOREIGN KEY

CREATE INDEX core_auditlogentry_actor_id_fca7de11 ON public.core_auditlogentry USING btree (actor_id);


-- ======================================================================
-- TABLE: directory_supportcontact
-- ======================================================================
CREATE TABLE directory_supportcontact (
    id bigint NOT NULL,
    name varchar(255) NOT NULL,
    description text NOT NULL,
    phone varchar(50) NOT NULL,
    address varchar(500) NOT NULL,
    email varchar(254) NOT NULL
);

ALTER TABLE directory_supportcontact ADD CONSTRAINT directory_supportcontact_pkey PRIMARY KEY (id);  -- PRIMARY KEY


-- ======================================================================
-- TABLE: django_admin_log
-- ======================================================================
CREATE TABLE django_admin_log (
    id integer NOT NULL,
    action_time timestamp with time zone NOT NULL,
    object_id text,
    object_repr varchar(200) NOT NULL,
    action_flag smallint NOT NULL,
    change_message text NOT NULL,
    content_type_id integer,
    user_id bigint NOT NULL
);

ALTER TABLE django_admin_log ADD CONSTRAINT django_admin_log_pkey PRIMARY KEY (id);  -- PRIMARY KEY
ALTER TABLE django_admin_log ADD CONSTRAINT django_admin_log_content_type_id_c4bce8eb_fk_django_co FOREIGN KEY (content_type_id) REFERENCES django_content_type(id) DEFERRABLE INITIALLY DEFERRED;  -- FOREIGN KEY
ALTER TABLE django_admin_log ADD CONSTRAINT django_admin_log_user_id_c564eba6_fk_accounts_user_id FOREIGN KEY (user_id) REFERENCES accounts_user(id) DEFERRABLE INITIALLY DEFERRED;  -- FOREIGN KEY
ALTER TABLE django_admin_log ADD CONSTRAINT django_admin_log_action_flag_check CHECK (action_flag >= 0);  -- CHECK

CREATE INDEX django_admin_log_content_type_id_c4bce8eb ON public.django_admin_log USING btree (content_type_id);
CREATE INDEX django_admin_log_user_id_c564eba6 ON public.django_admin_log USING btree (user_id);


-- ======================================================================
-- TABLE: django_cache_table
-- ======================================================================
CREATE TABLE django_cache_table (
    cache_key varchar(255) NOT NULL,
    value text NOT NULL,
    expires timestamp with time zone NOT NULL
);

ALTER TABLE django_cache_table ADD CONSTRAINT django_cache_table_pkey PRIMARY KEY (cache_key);  -- PRIMARY KEY

CREATE INDEX django_cache_table_expires ON public.django_cache_table USING btree (expires);


-- ======================================================================
-- TABLE: django_content_type
-- ======================================================================
CREATE TABLE django_content_type (
    id integer NOT NULL,
    app_label varchar(100) NOT NULL,
    model varchar(100) NOT NULL
);

ALTER TABLE django_content_type ADD CONSTRAINT django_content_type_pkey PRIMARY KEY (id);  -- PRIMARY KEY
ALTER TABLE django_content_type ADD CONSTRAINT django_content_type_app_label_model_76bd3d3b_uniq UNIQUE (app_label, model);  -- UNIQUE


-- ======================================================================
-- TABLE: django_migrations
-- ======================================================================
CREATE TABLE django_migrations (
    id bigint NOT NULL,
    app varchar(255) NOT NULL,
    name varchar(255) NOT NULL,
    applied timestamp with time zone NOT NULL
);

ALTER TABLE django_migrations ADD CONSTRAINT django_migrations_pkey PRIMARY KEY (id);  -- PRIMARY KEY


-- ======================================================================
-- TABLE: django_session
-- ======================================================================
CREATE TABLE django_session (
    session_key varchar(40) NOT NULL,
    session_data text NOT NULL,
    expire_date timestamp with time zone NOT NULL
);

ALTER TABLE django_session ADD CONSTRAINT django_session_pkey PRIMARY KEY (session_key);  -- PRIMARY KEY

CREATE INDEX django_session_expire_date_a5c62663 ON public.django_session USING btree (expire_date);
CREATE INDEX django_session_session_key_c0390e0f_like ON public.django_session USING btree (session_key varchar_pattern_ops);


-- ======================================================================
-- TABLE: milkbank_donorquestionnaire
-- ======================================================================
CREATE TABLE milkbank_donorquestionnaire (
    id bigint NOT NULL,
    currently_lactating_excess boolean NOT NULL,
    infant_age_months integer NOT NULL,
    consents_to_screening boolean NOT NULL,
    good_general_health boolean NOT NULL,
    being_treated_for_illness boolean NOT NULL,
    recent_fever_or_infection boolean NOT NULL,
    tested_positive_infectious_disease boolean NOT NULL,
    partner_tested_positive_or_at_risk boolean NOT NULL,
    recent_blood_transfusion boolean NOT NULL,
    recent_tattoo_piercing_needle_exposure boolean NOT NULL,
    travel_to_risk_area boolean NOT NULL,
    smokes_or_tobacco boolean NOT NULL,
    drinks_alcohol boolean NOT NULL,
    alcohol_frequency_details varchar(255) NOT NULL,
    uses_illicit_drugs boolean NOT NULL,
    on_prescription_medications boolean NOT NULL,
    medication_list text NOT NULL,
    uses_herbal_supplements boolean NOT NULL,
    uses_radioactive_or_radiologic boolean NOT NULL,
    vegan_without_b12 boolean NOT NULL,
    recent_live_virus_vaccine boolean NOT NULL,
    serology_photo varchar(100),
    submitted_at timestamp with time zone NOT NULL,
    request_id bigint NOT NULL
);

ALTER TABLE milkbank_donorquestionnaire ADD CONSTRAINT milkbank_donorquestionnaire_pkey PRIMARY KEY (id);  -- PRIMARY KEY
ALTER TABLE milkbank_donorquestionnaire ADD CONSTRAINT milkbank_donorquestionnaire_request_id_key UNIQUE (request_id);  -- UNIQUE
ALTER TABLE milkbank_donorquestionnaire ADD CONSTRAINT milkbank_donorquesti_request_id_c155d435_fk_milkbank_ FOREIGN KEY (request_id) REFERENCES milkbank_milkbankrequest(id) DEFERRABLE INITIALLY DEFERRED;  -- FOREIGN KEY
ALTER TABLE milkbank_donorquestionnaire ADD CONSTRAINT milkbank_donorquestionnaire_infant_age_months_check CHECK (infant_age_months >= 0);  -- CHECK


-- ======================================================================
-- TABLE: milkbank_facility
-- ======================================================================
CREATE TABLE milkbank_facility (
    id bigint NOT NULL,
    name varchar(255) NOT NULL,
    type varchar(100) NOT NULL,
    contact varchar(50) NOT NULL,
    address varchar(500) NOT NULL,
    operating_hours varchar(100) NOT NULL,
    donor_requirements text NOT NULL,
    recipient_requirements text NOT NULL,
    unavailable_donor_dates jsonb NOT NULL,
    unavailable_recipient_dates jsonb NOT NULL,
    is_operational boolean NOT NULL,
    capacity integer NOT NULL,
    booked_count integer NOT NULL,
    stock_level_ml integer NOT NULL,
    latitude double precision NOT NULL,
    longitude double precision NOT NULL
);

ALTER TABLE milkbank_facility ADD CONSTRAINT milkbank_facility_pkey PRIMARY KEY (id);  -- PRIMARY KEY
ALTER TABLE milkbank_facility ADD CONSTRAINT milkbank_facility_booked_count_check CHECK (booked_count >= 0);  -- CHECK
ALTER TABLE milkbank_facility ADD CONSTRAINT milkbank_facility_capacity_check CHECK (capacity >= 0);  -- CHECK
ALTER TABLE milkbank_facility ADD CONSTRAINT milkbank_facility_stock_level_ml_check CHECK (stock_level_ml >= 0);  -- CHECK


-- ======================================================================
-- TABLE: milkbank_milkbankrequest
-- ======================================================================
CREATE TABLE milkbank_milkbankrequest (
    id bigint NOT NULL,
    request_type varchar(20) NOT NULL,
    current_stage_index integer NOT NULL,
    current_sub_status varchar(30) NOT NULL,
    staff_message text NOT NULL,
    submitted_at timestamp with time zone NOT NULL,
    preferred_date date NOT NULL,
    preferred_time varchar(20) NOT NULL,
    attendance_confirmed boolean NOT NULL,
    counter_offer_date date,
    counter_offer_time varchar(20) NOT NULL,
    allocated_facility_id bigint NOT NULL,
    owner_id bigint NOT NULL
);

ALTER TABLE milkbank_milkbankrequest ADD CONSTRAINT milkbank_milkbankrequest_pkey PRIMARY KEY (id);  -- PRIMARY KEY
ALTER TABLE milkbank_milkbankrequest ADD CONSTRAINT milkbank_milkbankreq_allocated_facility_i_a8d7caef_fk_milkbank_ FOREIGN KEY (allocated_facility_id) REFERENCES milkbank_facility(id) DEFERRABLE INITIALLY DEFERRED;  -- FOREIGN KEY
ALTER TABLE milkbank_milkbankrequest ADD CONSTRAINT milkbank_milkbankrequest_owner_id_33f076b0_fk_accounts_user_id FOREIGN KEY (owner_id) REFERENCES accounts_user(id) DEFERRABLE INITIALLY DEFERRED;  -- FOREIGN KEY
ALTER TABLE milkbank_milkbankrequest ADD CONSTRAINT milkbank_milkbankrequest_current_stage_index_check CHECK (current_stage_index >= 0);  -- CHECK

CREATE INDEX milkbank_milkbankrequest_allocated_facility_id_a8d7caef ON public.milkbank_milkbankrequest USING btree (allocated_facility_id);
CREATE INDEX milkbank_milkbankrequest_owner_id_33f076b0 ON public.milkbank_milkbankrequest USING btree (owner_id);


-- ======================================================================
-- TABLE: milkbank_transactionrecord
-- ======================================================================
CREATE TABLE milkbank_transactionrecord (
    id bigint NOT NULL,
    type varchar(20) NOT NULL,
    facility_name varchar(255) NOT NULL,
    date date NOT NULL,
    status varchar(20) NOT NULL,
    owner_id bigint NOT NULL
);

ALTER TABLE milkbank_transactionrecord ADD CONSTRAINT milkbank_transactionrecord_pkey PRIMARY KEY (id);  -- PRIMARY KEY
ALTER TABLE milkbank_transactionrecord ADD CONSTRAINT milkbank_transaction_owner_id_d67caeea_fk_accounts_ FOREIGN KEY (owner_id) REFERENCES accounts_user(id) DEFERRABLE INITIALLY DEFERRED;  -- FOREIGN KEY

CREATE INDEX milkbank_transactionrecord_owner_id_d67caeea ON public.milkbank_transactionrecord USING btree (owner_id);


-- ======================================================================
-- TABLE: notifications_notificationitem
-- ======================================================================
CREATE TABLE notifications_notificationitem (
    id bigint NOT NULL,
    title varchar(255) NOT NULL,
    description text NOT NULL,
    category varchar(20) NOT NULL,
    created_at timestamp with time zone NOT NULL,
    is_read boolean NOT NULL,
    owner_id bigint NOT NULL
);

ALTER TABLE notifications_notificationitem ADD CONSTRAINT notifications_notificationitem_pkey PRIMARY KEY (id);  -- PRIMARY KEY
ALTER TABLE notifications_notificationitem ADD CONSTRAINT notifications_notifi_owner_id_b75cc032_fk_accounts_ FOREIGN KEY (owner_id) REFERENCES accounts_user(id) DEFERRABLE INITIALLY DEFERRED;  -- FOREIGN KEY

CREATE INDEX notifications_notificationitem_owner_id_b75cc032 ON public.notifications_notificationitem USING btree (owner_id);

