/**
 * NOKVO APEX legal documents — single source for the in-product Terms & Conditions
 * and Privacy Policy for the APEX deterministic-outbound product. Rendered inline
 * (v-html) wherever APEX needs to show the agreement (onboarding terms step,
 * settings, footer link) so users read the actual terms, not a dead external link.
 *
 * APEX differs from Nokvo One: it is a single-plan, outbound-only, prepaid product
 * (₹6,499/month subscription + a prepaid Call Credits wallet), and — per business
 * policy — ALL FEES ARE NON-REFUNDABLE (see Terms §6). Keep APEX_LEGAL_VERSIONS in
 * sync with the backend TERMS_VERSION when these change.
 *
 * Content is trusted, static, first-party HTML (safe to v-html).
 *
 * NOTE: This is a good-faith, plain-language agreement drafted for the APEX product;
 * it is NOT a substitute for review by qualified legal counsel before go-live.
 */

export const APEX_LEGAL_VERSIONS = {
  terms: '2026-07-04',
  privacy: '2026-07-04',
};

export const APEX_TERMS_OF_SERVICE_HTML = `
<h3>NOKVO APEX — Terms &amp; Conditions</h3>
<p class="legal-meta">Last Updated: July 4, 2026</p>

<p>These Terms &amp; Conditions ("Terms") govern your access to and use of NOKVO APEX ("APEX," the "Service"), an automated outbound AI voice-calling and lead-qualification platform provided by NEEDLES COUTURE AND COLLECTIVE PRIVATE LIMITED ("Nokvo," "we," "us," or "our").</p>
<p>By creating an APEX account, making payment, or otherwise accessing or using the Service, you ("Customer," "you," or "your") agree to be bound by these Terms. If you are accepting on behalf of a business, you represent that you are authorised to bind that business. If you do not agree, do not use the Service.</p>

<h4>1. The Service</h4>
<p><strong>1.1 What APEX does.</strong> APEX places automated outbound telephone calls on your behalf to contact lists you provide, speaks with the person using an AI voice agent, asks a fixed questionnaire you configure, scores and qualifies the response, and records the call and its transcript so you can review, claim, and follow up on leads.</p>
<p><strong>1.2 Licence.</strong> Subject to these Terms and payment of applicable fees, we grant you a limited, non-exclusive, non-transferable, revocable right to use the Service for your internal business purposes.</p>
<p><strong>1.3 Changes to the Service.</strong> We may modify, update, or discontinue features at any time and will give reasonable notice of any material change to core functionality.</p>
<p><strong>1.4 Operational limits.</strong> To protect call quality, carrier relationships, and telecom compliance, the Service enforces operational limits. These include: <strong>one calling campaign per account runs at a time</strong> — a new campaign can be started once the current campaign completes or you cancel it; dialling is restricted to the 9:00 AM–7:00 PM IST window on your configured working days; and any per-day call caps you configure are applied to placements. We may adjust operational limits from time to time and will give reasonable notice of material changes.</p>

<h4>2. Eligibility, Account &amp; Members</h4>
<p><strong>2.1 Business use only.</strong> APEX is offered strictly for lawful business use by persons aged 18 or over. You must provide accurate registration and business verification (KYC) information and keep it current.</p>
<p><strong>2.2 Account security.</strong> You are responsible for all activity under your account and for safeguarding your credentials. Notify us immediately of any unauthorised use.</p>
<p><strong>2.3 Members.</strong> You may invite team members to claim and work qualified leads. You are responsible for your members' access, conduct, and compliance with these Terms.</p>

<h4>3. Your Compliance Obligations (Outbound Calling)</h4>
<p><strong>3.1 You are the caller.</strong> APEX makes calls at your instruction and on your behalf. You are solely responsible for the legality of your calling campaigns, your contact lists, and the consent basis for contacting each recipient.</p>
<p><strong>3.2 Telecom &amp; DND compliance.</strong> You must comply with all applicable telecommunications and telemarketing laws and regulations, including the regulations of the Telecom Regulatory Authority of India (TRAI), the Telecom Commercial Communications Customer Preference Regulations (DND/NCPR), DLT registration requirements for commercial communications, and permitted calling-time windows (APEX restricts dialling to 9:00 AM–7:00 PM IST). APEX provides DND/NCPR scrubbing and time-window controls as tools to assist your compliance, but using them does not transfer legal responsibility to Nokvo — you remain the entity legally responsible for each call.</p>
<p><strong>3.3 Consent &amp; lawful lists.</strong> You represent and warrant that you have a lawful basis and any legally required consent to call each number on your lists and to record and process the resulting conversation, and that your lists were obtained lawfully and do not target persons who have opted out.</p>
<p><strong>3.4 AI &amp; recording disclosure.</strong> Calls are recorded, transcribed, and conducted by an automated AI agent. Where required by law, you are responsible for ensuring recipients are informed that they are speaking with an automated system and that the call is recorded. APEX can be configured to make such disclosures; enabling and maintaining them is your responsibility.</p>
<p><strong>3.5 Prohibited use.</strong> You must not use APEX to place fraudulent, deceptive, harassing, threatening, or unlawful calls; to spoof caller identity; to contact numbers without a lawful basis; to send content that is defamatory, obscene, or infringing; or to reverse-engineer, disrupt, overload, or build a competing product from the Service.</p>

<h4>4. Artificial Intelligence Disclaimers</h4>
<p><strong>4.1 Nature of AI.</strong> APEX relies on speech recognition, large language models, and speech synthesis. Because these systems are probabilistic, the agent may mishear, misinterpret, mis-score, or produce inaccurate or unintended output, and lead-qualification results are indicative, not guaranteed.</p>
<p><strong>4.2 Your oversight.</strong> You are responsible for reviewing call outcomes, scores, transcripts, and notes before acting on them. You agree that Nokvo is not liable for business losses, missed leads, wrongful qualification/disqualification, reputational harm, or claims arising from statements made or actions taken by the AI agent during calls.</p>
<p><strong>4.3 No results guarantee.</strong> We do not warrant any particular connection rate, answer rate, qualification rate, conversion, or business outcome.</p>

<h4>5. Fees, Subscription &amp; Call Credits</h4>
<p><strong>5.1 Subscription.</strong> APEX is offered on a recurring monthly subscription of ₹6,499 per month (plus applicable taxes), billed automatically to your payment method through our payment processor (Razorpay) on each billing cycle until cancelled.</p>
<p><strong>5.2 Call Credits (prepaid).</strong> Outbound calling consumes prepaid Call Credits from your APEX wallet. Credits are purchased in advance (at onboarding and via top-ups) at the applicable slab rate, where one credit is valued at ₹1. Every connected call deducts credits at the then-current selling rate; calls that are not answered/connected are not charged. Credits have no cash value, are not a deposit, are non-transferable, and are consumed on a first-purchased-first-used basis. Each purchase (onboarding or top-up) is subject to a per-transaction limit — currently a minimum of 100 minutes and a maximum of 100,000 minutes' worth of credits per purchase — which we may adjust from time to time.</p>
<p><strong>5.3 Authorisation to charge.</strong> By providing a payment method you authorise us and our payment processor to charge the subscription fee each cycle and any top-ups you initiate. Taxes are your responsibility except taxes on Nokvo's net income.</p>
<p><strong>5.4 Non-payment.</strong> If a subscription payment fails, or if your Call Credit balance is exhausted, we may suspend calling and other paid functions until payment succeeds or credits are topped up.</p>
<p><strong>5.5 Balance timing.</strong> Your Call Credit balance is checked when each call is placed. Calls already in progress — or placed in the brief interval before an exhausted balance is registered — are allowed to complete and are charged in full, which may take your balance marginally below zero. You remain responsible for that usage; any negative amount is recovered from your next top-up.</p>

<h4>6. No Refunds</h4>
<p><strong>6.1 All fees are non-refundable.</strong> ALL PAYMENTS TO NOKVO FOR APEX — INCLUDING MONTHLY SUBSCRIPTION FEES, PREPAID CALL CREDITS, AND TOP-UPS — ARE FINAL AND NON-REFUNDABLE, IN WHOLE OR IN PART, EXCEPT WHERE A REFUND IS REQUIRED BY APPLICABLE LAW.</p>
<p><strong>6.2 No pro-rata on cancellation.</strong> You may cancel your subscription at any time from within the Service; cancellation takes effect at the end of your current paid billing cycle. You will not be charged again after that cycle, but the fee already paid for the current cycle is not refunded or pro-rated, and your access continues until the cycle ends.</p>
<p><strong>6.3 Unused Call Credits.</strong> Purchased Call Credits are non-refundable and non-redeemable for cash, including any unused balance remaining on cancellation, suspension, or termination.</p>
<p><strong>6.4 No refund for AI variance or downtime.</strong> Fees and consumed credits are not refunded on the basis of AI mishearing/mis-scoring, unanswered calls, recipient behaviour, third-party network or carrier issues, or service interruptions, save as required by law.</p>
<p><strong>6.5 Exceptional adjustments.</strong> Any goodwill credit or adjustment we may choose to grant in a specific case is at our sole discretion, is not an admission of any obligation, and does not create a right to a refund in any other case.</p>

<h4>7. Customer Data &amp; Privacy</h4>
<p><strong>7.1 Your data.</strong> You retain ownership of the contact lists, questionnaires, and business information you provide ("Customer Data"). You represent that you have all rights and consents necessary to provide it and to have it processed by APEX.</p>
<p><strong>7.2 Our processing.</strong> You grant us a limited right to process, transmit, and store Customer Data solely to provide the Service and maintain its security. We act as your data processor for the personal data of call recipients. Our practices are described in the APEX Privacy Policy, which you accept by using the Service.</p>
<p><strong>7.3 No training on your data.</strong> We do not sell Customer Data and do not use your call recordings, transcripts, phone lists, or business documents to train public foundation models. We may use aggregated, de-identified metrics to operate and improve the Service.</p>

<h4>8. Intellectual Property &amp; Feedback</h4>
<p>Nokvo and its licensors own all rights in the Service, including its software, models, interfaces, and branding. If you send us feedback or suggestions, you grant us a royalty-free, worldwide, perpetual licence to use them without obligation to you.</p>

<h4>9. Suspension &amp; Termination</h4>
<p><strong>9.1 By you.</strong> You may stop using and cancel the Service at any time, subject to §6 (No Refunds).</p>
<p><strong>9.2 By us.</strong> We may suspend or terminate your access immediately, without refund, if you breach these Terms (including §3 Compliance), if your use creates legal, security, or reputational risk, if required fees are unpaid, or as required by law or a telecom provider.</p>
<p><strong>9.3 Effect.</strong> On termination your right to use the Service ends. We will make your data available for export for 30 days, after which it is deleted per our retention policy. Sections that by their nature should survive (Fees, No Refunds, IP, Disclaimers, Limitation of Liability, Indemnity, Governing Law) survive termination.</p>

<h4>10. Disclaimers</h4>
<p>THE SERVICE IS PROVIDED "AS IS" AND "AS AVAILABLE". TO THE MAXIMUM EXTENT PERMITTED BY LAW, NOKVO DISCLAIMS ALL WARRANTIES, EXPRESS OR IMPLIED, INCLUDING MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, AND NON-INFRINGEMENT, AND DOES NOT WARRANT THAT THE SERVICE WILL BE UNINTERRUPTED, ERROR-FREE, OR THAT AI OUTPUTS OR CALL OUTCOMES WILL BE ACCURATE OR RELIABLE. APEX depends on third-party telephony carriers, AI model providers, and cloud infrastructure; we are not responsible for their outages, delays, or changes.</p>

<h4>11. Limitation of Liability</h4>
<p>TO THE MAXIMUM EXTENT PERMITTED BY LAW, NOKVO AND ITS AFFILIATES WILL NOT BE LIABLE FOR ANY INDIRECT, INCIDENTAL, SPECIAL, CONSEQUENTIAL, OR PUNITIVE DAMAGES, OR FOR LOST PROFITS, LOST LEADS, LOST DATA, OR BUSINESS INTERRUPTION. NOKVO'S TOTAL AGGREGATE LIABILITY ARISING OUT OF OR RELATING TO THE SERVICE WILL NOT EXCEED THE AMOUNTS YOU PAID TO NOKVO FOR APEX IN THE THREE (3) MONTHS PRECEDING THE EVENT GIVING RISE TO THE CLAIM.</p>

<h4>12. Indemnification</h4>
<p>You will indemnify and hold harmless Nokvo, its officers, employees, and agents from any claim, liability, loss, or expense (including reasonable legal fees) arising out of: your use of the Service; your calling campaigns and contact lists; your violation of these Terms or of any telecom, marketing, consumer-protection, or privacy law (including TRAI/DND/DLT); your failure to obtain required consent for calling, recording, or AI processing; or any claim by a call recipient or third party relating to a call you caused APEX to place.</p>

<h4>13. Governing Law &amp; Disputes</h4>
<p>These Terms are governed by the laws of India. Any dispute will be finally resolved by arbitration in Hyderabad, Telangana, India under the Arbitration and Conciliation Act, 1996, before a sole arbitrator, in English. Subject to arbitration, the courts of Hyderabad, Telangana, India have exclusive jurisdiction.</p>

<h4>14. General</h4>
<p>We may update these Terms and will give reasonable notice of material changes; continued use after the effective date is acceptance. You may not assign these Terms without our consent; we may assign them in a merger or sale of assets. If any provision is unenforceable, the rest remains in effect. These Terms and the APEX Privacy Policy are the entire agreement for APEX. We may send notices electronically (email or in-product). Neither party is liable for delays caused by events beyond its reasonable control.</p>

<h4>15. Contact</h4>
<p>Questions about these Terms may be sent to NEEDLES COUTURE AND COLLECTIVE PRIVATE LIMITED at <a href="mailto:officialnokvo@nokvo.org">officialnokvo@nokvo.org</a>.</p>
`;

export const APEX_PRIVACY_POLICY_HTML = `
<h3>NOKVO APEX — Privacy Policy</h3>
<p class="legal-meta">Last Updated: July 4, 2026</p>

<p>This Privacy Policy explains how NEEDLES COUTURE AND COLLECTIVE PRIVATE LIMITED ("Nokvo," "we," "us") collects, uses, and protects information in connection with NOKVO APEX ("APEX"), our automated outbound AI voice-calling and lead-qualification service. It distinguishes between data we collect about our direct business customers (Account Data) and data we process on our customers' behalf about the people they call (Customer Data).</p>

<h4>1. Our Roles</h4>
<ul>
  <li><strong>Data Fiduciary / Controller:</strong> We are the controller of Account Data of our direct customers and their invited members.</li>
  <li><strong>Data Processor:</strong> We are the processor of Customer Data — the personal data of call recipients — which we process only on our customer's instructions to deliver APEX.</li>
</ul>

<h4>2. Information We Collect</h4>
<p><strong>A. Account Data (about our customers).</strong></p>
<ul>
  <li><strong>Registration &amp; contact:</strong> name, email, phone number, company name, and account role.</li>
  <li><strong>Business verification (KYC):</strong> business identity details and documents you upload for onboarding and compliance.</li>
  <li><strong>Authentication:</strong> login credentials, tokens, and (where enabled) multi-factor authentication data.</li>
  <li><strong>Billing:</strong> subscription and Call Credit transaction records and payment references (card/bank details are handled by our payment processor, not stored by us).</li>
  <li><strong>Usage &amp; device:</strong> IP address, log data, cookies used to keep you signed in and secure the platform, and diagnostic/latency telemetry.</li>
</ul>
<p><strong>B. Customer Data (about the people you call), processed on your behalf.</strong></p>
<ul>
  <li><strong>Contact list data:</strong> recipient phone numbers and names/labels you upload.</li>
  <li><strong>Call data:</strong> call audio recordings and call metadata (duration, timestamps, connection status).</li>
  <li><strong>Transcripts &amp; outcomes:</strong> speech-to-text transcripts, questionnaire answers, lead scores, call notes, and follow-up status.</li>
</ul>

<h4>3. How We Use Information</h4>
<p>We use Account Data to provide and secure APEX, verify businesses, process subscription and Call Credit billing, send administrative and security notices, and improve reliability and performance. We process Customer Data solely to place calls you instruct, conduct and record the AI conversation, transcribe and score responses, apply DND/NCPR scrubbing and calling-time controls, and surface qualified leads and notes to you and your members.</p>

<h4>4. Legal Bases</h4>
<p>Where applicable we rely on: performance of our contract with you; our legitimate interests in operating and securing the Service; compliance with legal obligations; and, for Customer Data, the lawful basis and consent that you (as controller) are responsible for establishing with call recipients.</p>

<h4>5. No Sale; No Model Training on Your Data</h4>
<ul>
  <li>We do not sell or rent personal data, and we do not use Customer Data for advertising.</li>
  <li>We do not use your call recordings, transcripts, contact lists, or uploaded documents to train public foundation models.</li>
  <li>We may use aggregated, de-identified metrics and non-identifiable linguistic/performance patterns to operate and improve APEX (including localisation of Indian-language speech quality).</li>
</ul>

<h4>6. Your Compliance as Controller</h4>
<p>Because you decide whom APEX calls and why, you are responsible for having a lawful basis and any required consent to call, record, and process each recipient; for providing recipients any required notices (including AI and recording disclosures); and for ensuring your contact lists were obtained lawfully and honour opt-outs.</p>

<h4>7. Subprocessors</h4>
<p>To deliver APEX we use trusted third-party providers, including:</p>
<ul>
  <li><strong>Telephony:</strong> Plivo (outbound calling and call media).</li>
  <li><strong>Speech (STT/TTS):</strong> Sarvam AI for speech-to-text and text-to-speech.</li>
  <li><strong>AI models:</strong> Microsoft Azure OpenAI for language understanding, scoring, and generation.</li>
  <li><strong>Cloud infrastructure:</strong> Microsoft Azure (database, cache, blob storage, key vault), hosted in designated regions.</li>
  <li><strong>Payments:</strong> Razorpay for subscription and Call Credit transactions.</li>
</ul>
<p>We maintain appropriate contractual and security safeguards with subprocessors and may update this list from time to time; a current list is available on request.</p>

<h4>8. Data Retention</h4>
<ul>
  <li><strong>Transcripts &amp; recordings:</strong> retained on a rolling basis (call transcripts are automatically deleted after approximately 30 days), subject to your settings and our legal obligations.</li>
  <li><strong>In-call memory:</strong> live conversational state is held only for the call and a short window after it, then discarded.</li>
  <li><strong>Operational caches:</strong> transient service lookups (such as the wallet-balance figures used to gate dialling) are held in short-lived caches that expire automatically within seconds and are not separately retained.</li>
  <li><strong>Lead &amp; account records:</strong> retained while your account is active and as needed for billing, audit, and legal compliance.</li>
</ul>

<h4>9. Security</h4>
<ul>
  <li>Encryption in transit (TLS/HTTPS/WSS) and at rest.</li>
  <li>Sensitive credentials and secrets managed in Azure Key Vault.</li>
  <li>Role-based access control, tenant isolation, and multi-factor authentication for privileged access.</li>
  <li>On a confirmed security incident affecting personal data, we will notify affected customers without undue delay and as required by applicable law.</li>
</ul>

<h4>10. Your Rights</h4>
<p>Subject to applicable law (including India's Digital Personal Data Protection Act, 2023), you may request access to, correction of, or deletion of your Account Data, and may withdraw consent or object to certain processing. To exercise these rights, contact us at the address below.</p>
<p><strong>Note for call recipients:</strong> if you received a call from an APEX AI agent, the business that placed the call is the controller of your data — please direct requests to that business. We will assist that business as its processor.</p>

<h4>11. International Transfers, Legal Requests &amp; Business Changes</h4>
<p>Data may be processed in regions where our providers operate, protected by appropriate safeguards. We may disclose information where required by law or valid legal process, and, where permitted, will notify affected customers. If Nokvo is involved in a merger, acquisition, or asset sale, information may be transferred as part of that transaction subject to confidentiality.</p>

<h4>12. Changes</h4>
<p>We may update this Policy to reflect changes in the Service, providers, or law, and will notify you of significant changes by email or in-product notice.</p>

<h4>13. Contact &amp; Grievances</h4>
<p>For privacy questions, requests, or grievances under the Digital Personal Data Protection Act, 2023, contact NEEDLES COUTURE AND COLLECTIVE PRIVATE LIMITED at <a href="mailto:officialnokvo@nokvo.org">officialnokvo@nokvo.org</a>. We will respond within the timelines required by applicable law.</p>
`;
