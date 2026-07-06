/**
 * Nokvo legal documents — single source for the in-product Terms of Service and
 * Privacy Policy. Rendered inline at the onboarding "terms" step so admins read
 * the actual agreement before accepting (not a dead external link), and reusable
 * anywhere else the docs need to be shown.
 *
 * Content is trusted, static, first-party HTML (safe to v-html). Keep
 * LEGAL_VERSIONS in sync with the backend TERMS_VERSION when these change.
 */

export const LEGAL_VERSIONS = {
  terms: '2026-07-04',
  privacy: '2026-07-04',
};

export const TERMS_OF_SERVICE_HTML = `
<h3>Nokvo Terms of Service</h3>
<p class="legal-meta">Last Updated: July 4, 2026</p>

<p>Welcome to Nokvo! These Terms of Service ("Terms") govern your access to and use of the Nokvo One platform, website, APIs, and related services (collectively, the "Services") provided by NEEDLES COUTURE AND COLLECTIVE PRIVATE LIMITED ("Nokvo," "we," "us," or "our").</p>

<p>By registering for an account, executing an Order Form that references these Terms, or otherwise accessing or using the Services, you ("Customer," "you," or "your") agree to be bound by these Terms. If you are entering into these Terms on behalf of a company or other legal entity, you represent that you have the authority to bind such entity to these Terms. If you do not agree to these Terms, you may not use the Services.</p>

<h4>1. Overview of the Services</h4>
<p>Nokvo provides an enterprise-grade AI voice agent platform designed to automate and enhance business communications, including real-time speech-to-text, LLM-powered responses, text-to-speech synthesis, and CRM/third-party integrations.</p>
<p><strong>1.1 Access and Use.</strong> Subject to your compliance with these Terms and payment of applicable fees, Nokvo grants you a limited, non-exclusive, non-transferable, and revocable right to access and use the Services for your internal business purposes.</p>
<p><strong>1.2 Modifications to the Services.</strong> We reserve the right to modify, update, or discontinue features of the Services at any time. We will provide reasonable advance notice of any material deprecation of core features.</p>
<p><strong>1.3 Beta Features.</strong> Nokvo may designate certain features as Beta Features or Early Access. Beta Features are provided "as is," may be modified or discontinued at any time, and are excluded from any service commitments, SLAs, or warranties.</p>

<h4>2. Account Registration and Security</h4>
<p><strong>2.1 Account Creation.</strong> You must provide accurate, complete, and current information when registering an account. You are responsible for all activities that occur under your account.</p>
<p><strong>2.2 Security.</strong> You must maintain the confidentiality of your login credentials and integration keys (e.g., API keys, OAuth tokens). You agree to immediately notify us of any unauthorized use or suspected breach of security. Nokvo is not liable for any loss or damage arising from your failure to safeguard your credentials.</p>

<h4>3. Acceptable Use and Compliance</h4>
<p><strong>3.1 Prohibited Activities.</strong> You agree not to:</p>
<ul>
  <li>Reverse engineer, decompile, or disassemble any part of the Services.</li>
  <li>Use the Services to build a competitive product.</li>
  <li>Interfere with or disrupt the integrity or performance of the Services.</li>
  <li>Attempt to gain unauthorized access to the Services or related systems.</li>
  <li>Use the Services for fraudulent, illegal, or harassing activities, including "spoofing" caller IDs or transmitting malware.</li>
</ul>
<p><strong>3.2 Telecom and Communication Compliance.</strong> When using Nokvo to make outbound calls, send SMS, or process inbound communications, you are solely responsible for complying with all applicable telecommunications, marketing, and privacy laws. This includes, but is not limited to: the Telephone Consumer Protection Act (TCPA) in the US; the Telemarketing Sales Rule (TSR); Do Not Call (DNC) registry requirements; A2P 10DLC registration and compliance for SMS messaging; TRAI regulations and DLT registration for SMS/voice in India; and the General Data Protection Regulation (GDPR) and the Digital Personal Data Protection Act (DPDP Act).</p>
<p><strong>3.3 Consent and Call Recording.</strong> The Services involve recording, transcribing, and processing human speech. You represent and warrant that you have obtained all necessary consents from end-users to record calls, process their voice data, and interact with an AI agent. Nokvo processes this data solely as your processor/service provider.</p>
<p><strong>3.4 Usage Limits and Fair Use.</strong> Nokvo may enforce reasonable usage limits, rate limits, concurrency limits, storage limits, and fair-use restrictions to protect platform stability and ensure equitable access for all customers.</p>
<p><strong>3.5 AI Disclosure.</strong> Where required by applicable law, you are responsible for disclosing to your end-users that they are interacting with an automated AI agent rather than a human. Nokvo's agents can be configured to make such a disclosure; you are responsible for enabling and maintaining it as your jurisdiction requires.</p>

<h4>4. Artificial Intelligence Disclaimers</h4>
<p><strong>4.1 Nature of AI.</strong> You acknowledge that the Services rely on advanced artificial intelligence, machine learning, and large language models (LLMs). Due to the probabilistic nature of AI, the Services may occasionally generate inaccurate, inappropriate, or unintended responses ("Hallucinations").</p>
<p><strong>4.2 Human Oversight and High-Risk Uses.</strong> It is your responsibility to monitor, review, and supervise the outputs of the AI agents. You acknowledge that AI-generated outputs may require human review and should not be solely relied upon for legal, financial, medical, employment, safety-critical, or other high-risk decisions. You agree that Nokvo is not liable for any business losses, reputational damage, or legal claims arising from statements made, actions taken, or information provided by the AI agents during interactions with your end-users.</p>

<h4>5. Customer Data and Privacy</h4>
<p><strong>5.1 Ownership and Warranty.</strong> You retain all rights, title, and interest in and to all data, knowledge base documents, prompts, and information you submit to the Services ("Customer Data"). You represent and warrant that you have all rights necessary to upload, process, and use Customer Data within the Services, and that such data does not infringe on any third-party intellectual property or privacy rights.</p>
<p><strong>5.2 License to Process.</strong> You grant Nokvo a non-exclusive, worldwide, royalty-free right to process, transmit, and store Customer Data solely to the extent necessary to provide the Services, fulfill our obligations under these Terms, and maintain platform security.</p>
<p><strong>5.3 No Training on Customer Data.</strong> Nokvo does not use Customer Data, call recordings, transcripts, or uploaded proprietary documents to train public foundation models. We may use aggregated, de-identified telemetry and linguistic patterns to optimize platform latency and speech model quality, as detailed in our Privacy Policy.</p>
<p><strong>5.4 Privacy Policy.</strong> Our data collection and processing practices are described in our Privacy Policy. By using the Services, you acknowledge our Privacy Policy.</p>
<p><strong>5.5 Security Incidents.</strong> In the event of a confirmed security incident affecting Customer Data, Nokvo will notify Customer without undue delay and take reasonable steps to investigate, mitigate, and remediate the incident in accordance with applicable law.</p>
<p><strong>5.6 Data Processing Addendum (DPA).</strong> If your use of the Services requires a Data Processing Addendum (DPA) to comply with applicable data protection laws (such as GDPR), please contact us to execute our standard DPA, which will then be incorporated into these Terms by reference.</p>
<p><strong>5.7 Subprocessors.</strong> Customer acknowledges and agrees that Nokvo may engage third-party subprocessors and service providers to assist in providing the Services. Nokvo will remain responsible for the acts and omissions of its subprocessors to the extent required by applicable law and any applicable DPA.</p>

<h4>6. Fees and Payment</h4>
<p><strong>6.1 Subscription and Usage Fees.</strong> The Services are billed on a subscription basis and/or a usage-based model (e.g., per-minute of voice processing, telephony costs, LLM tokens), as set out in your selected plan or applicable Order Form.</p>
<p><strong>6.2 Prepaid Voice Minutes.</strong> Voice minutes are purchased in advance as prepaid bundles (at onboarding or via top-ups) at the applicable bracket rate, and eligible calls consume the resulting prepaid balance. Each bundle purchase is subject to a per-transaction limit — currently a minimum of 100 and a maximum of 100,000 minutes per purchase — which we may adjust from time to time. Prepaid balances have no cash value, are non-transferable, and are consumed on a first-purchased-first-used basis. The balance is checked when a call starts; calls already in progress when the balance is exhausted are allowed to complete and are charged in full, which may take the balance marginally below zero — such usage remains payable and is recovered from your next purchase.</p>
<p><strong>6.3 Invoicing and Payment.</strong> You agree to provide a valid payment method. By providing payment information, you authorize Nokvo to automatically charge all applicable fees. If your payment fails, we may suspend your access to the Services until payment is successfully processed.</p>
<p><strong>6.4 Taxes.</strong> Fees are exclusive of all taxes, levies, or duties imposed by taxing authorities. You are responsible for payment of all such taxes (excluding taxes based on Nokvo's net income).</p>

<h4>7. Intellectual Property</h4>
<p><strong>7.1 Nokvo Ownership.</strong> Nokvo and its licensors retain all rights, title, and interest in and to the Services, including all software, algorithms, user interfaces, branding, and underlying technology.</p>
<p><strong>7.2 Feedback.</strong> If you provide us with any suggestions, enhancement requests, or other feedback regarding the Services, you grant Nokvo a royalty-free, worldwide, perpetual license to use and incorporate such feedback into the Services.</p>

<h4>8. Confidentiality</h4>
<p><strong>8.1 Definition.</strong> "Confidential Information" means any non-public information disclosed by one party to the other, designated as confidential or which reasonably should be understood to be confidential.</p>
<p><strong>8.2 Protection.</strong> The receiving party will protect the disclosing party's Confidential Information using the same degree of care it uses for its own similar information, but no less than reasonable care, and will only use it to exercise rights or fulfill obligations under these Terms.</p>

<h4>9. Term, Suspension, and Termination</h4>
<p><strong>9.1 Term.</strong> These Terms commence on the date you first accept them and remain in effect until all subscriptions expire or are terminated.</p>
<p><strong>9.2 Service Suspension.</strong> Nokvo may suspend access to the Services immediately if: Customer's use poses a security risk to the Services or other customers; Customer violates applicable laws or regulations; Customer's usage materially degrades platform performance; or required fees remain unpaid.</p>
<p><strong>9.3 Termination for Cause.</strong> Either party may terminate these Terms if the other party materially breaches them and fails to cure the breach within 30 days of written notice. Nokvo may terminate your account immediately without notice if you violate the Acceptable Use policy (Section 3).</p>
<p><strong>9.4 Effect of Termination.</strong> Upon termination, your right to access the Services will immediately cease. We will provide you with an opportunity to export your Customer Data for a period of 30 days post-termination, after which it will be deleted in accordance with our data retention policies.</p>
<p><strong>9.5 Survival.</strong> Sections relating to Fees, Intellectual Property, Confidentiality, Disclaimers, Limitation of Liability, Indemnification, and any provisions which by their nature should survive termination shall survive any termination or expiration of these Terms.</p>

<h4>10. Disclaimers</h4>
<p><strong>10.1 General Disclaimer.</strong> THE SERVICES ARE PROVIDED ON AN "AS IS" AND "AS AVAILABLE" BASIS. NOKVO EXPRESSLY DISCLAIMS ALL WARRANTIES OF ANY KIND, WHETHER EXPRESS OR IMPLIED, INCLUDING WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, AND NON-INFRINGEMENT. NOKVO DOES NOT WARRANT THAT THE SERVICES WILL BE UNINTERRUPTED, ERROR-FREE, SECURE, OR THAT THE AI OUTPUTS WILL ALWAYS BE ACCURATE OR RELIABLE.</p>
<p><strong>10.2 Third-Party Provider Disclaimer.</strong> Certain features rely on third-party providers, including cloud infrastructure providers, telecommunications carriers, AI model providers, and integration partners. Nokvo is not responsible for interruptions, inaccuracies, delays, outages, or changes caused by third-party services.</p>
<p><strong>10.3 Service Level Agreement (SLA).</strong> Unless expressly stated in an executed Order Form or Service Level Agreement (SLA), Nokvo does not guarantee any specific uptime, response time, availability, or service performance metrics.</p>

<h4>11. Limitation of Liability</h4>
<p>TO THE MAXIMUM EXTENT PERMITTED BY LAW, IN NO EVENT SHALL NOKVO OR ITS AFFILIATES BE LIABLE FOR ANY INDIRECT, INCIDENTAL, SPECIAL, CONSEQUENTIAL, OR PUNITIVE DAMAGES, INCLUDING LOST PROFITS, LOST REVENUES, LOST DATA, OR BUSINESS INTERRUPTION.</p>
<p>NOKVO'S TOTAL CUMULATIVE LIABILITY ARISING OUT OF OR RELATING TO THESE TERMS SHALL NOT EXCEED THE TOTAL AMOUNTS PAID BY YOU TO NOKVO FOR THE SERVICES IN THE TWELVE (12) MONTHS PRECEDING THE EVENT GIVING RISE TO THE LIABILITY.</p>

<h4>12. Indemnification</h4>
<p>You agree to indemnify, defend, and hold harmless Nokvo, its officers, directors, employees, and agents from and against any claims, liabilities, damages, judgments, awards, losses, costs, or expenses (including reasonable attorneys' fees) arising out of or relating to:</p>
<ul>
  <li>Your violation of these Terms.</li>
  <li>Your violation of applicable laws (including TCPA, DNC, TRAI/DLT, and privacy regulations).</li>
  <li>Your failure to obtain legally required consent for call recording or AI processing.</li>
  <li>Any third-party claim alleging that your Customer Data infringes or misappropriates their intellectual property or privacy rights.</li>
</ul>

<h4>13. General Provisions</h4>
<p><strong>13.1 Governing Law and Dispute Resolution.</strong> These Terms shall be governed by and construed in accordance with the laws of India. Any dispute arising out of or relating to these Terms shall be finally resolved by arbitration in Hyderabad, Telangana, India, in accordance with the Arbitration and Conciliation Act, 1996. The arbitration shall be conducted by a sole arbitrator appointed mutually by the parties. The language of arbitration shall be English. Subject to the foregoing, any legal proceedings shall be subject to the exclusive jurisdiction of the courts located in Hyderabad, Telangana, India.</p>
<p><strong>13.2 Assignment.</strong> You may not assign these Terms without our prior written consent. Nokvo may assign these Terms in connection with a merger, acquisition, or sale of all or substantially all of its assets.</p>
<p><strong>13.3 Severability.</strong> If any provision of these Terms is found to be unenforceable or invalid, that provision will be limited or eliminated to the minimum extent necessary so that these Terms will otherwise remain in full force and effect.</p>
<p><strong>13.4 Entire Agreement and Conflict.</strong> These Terms, along with any applicable Order Forms and the Privacy Policy, constitute the entire agreement between you and Nokvo regarding the Services. In the event of a conflict between these Terms and an executed Order Form, the Order Form shall control solely with respect to the applicable Services.</p>
<p><strong>13.5 Export Control and Sanctions.</strong> Customer may not use the Services in violation of applicable export control, sanctions, or trade laws of India, the United States, or other applicable jurisdictions.</p>
<p><strong>13.6 Force Majeure.</strong> Neither party shall be liable for delays or failures resulting from causes beyond its reasonable control, including natural disasters, internet outages, telecommunications failures, governmental actions, labor disputes, or cyberattacks.</p>
<p><strong>13.7 Publicity Rights.</strong> Customer grants Nokvo the right to use its name and logo solely for identifying Customer as a user of the Services, unless Customer requests otherwise in writing.</p>
<p><strong>13.8 Electronic Communications.</strong> Customer agrees that Nokvo may provide notices, disclosures, invoices, and other communications electronically, including via email or through the Services.</p>
<p><strong>13.9 Updates to Terms.</strong> Nokvo may modify these Terms from time to time. If we make material changes, we will provide reasonable notice through the Services, email, or other appropriate means. Continued use of the Services after the effective date of the revised Terms constitutes acceptance of the updated Terms.</p>

<h4>14. Contact</h4>
<p>Questions about these Terms may be directed to NEEDLES COUTURE AND COLLECTIVE PRIVATE LIMITED at <a href="mailto:officialnokvo@nokvo.org">officialnokvo@nokvo.org</a>.</p>
`;

export const PRIVACY_POLICY_HTML = `
<h3>Nokvo Privacy Policy</h3>
<p class="legal-meta">Last Updated: July 4, 2026</p>

<p>Welcome to Nokvo ("we," "our," or "us"). We provide Nokvo One, an enterprise-grade AI voice agent platform designed to automate and enhance business communications.</p>
<p>This Privacy Policy explains how we collect, use, disclose, and safeguard your information when you visit our website, use our application, or interact with our services. As a B2B Software-as-a-Service (SaaS) provider, it is critical to distinguish between data we collect for our own purposes (Account Data) and data we process on behalf of our business customers (Customer Data).</p>

<h4>1. Our Role in Data Processing</h4>
<p>Under applicable data protection laws:</p>
<ul>
  <li><strong>Data Controller:</strong> We are the data controller for the Account Data of our direct customers (businesses who subscribe to Nokvo).</li>
  <li><strong>Data Processor (Service Provider):</strong> We are the data processor for Customer Data, which includes the personal information of our customers' end-users (e.g., callers interacting with the AI agent). We only process this data according to our customers' instructions.</li>
</ul>

<h4>2. Information We Collect</h4>
<p><strong>A. Information Collected as a Data Controller (Account Data).</strong> When you register for a Nokvo account, we collect:</p>
<ul>
  <li><strong>Contact Details:</strong> Name, email address, company name, and phone number.</li>
  <li><strong>Authentication Data:</strong> Authentication credentials, authentication tokens, OAuth tokens (e.g., Google, Zoho), and MFA/WebAuthn credentials.</li>
  <li><strong>Billing Information:</strong> Payment details and billing addresses (processed via secure third-party payment gateways).</li>
  <li><strong>Usage &amp; Device Data:</strong> IP addresses, browser types, log data, cookies and similar technologies used to keep you signed in and secure the platform, and OpenTelemetry observability metrics to ensure platform stability.</li>
</ul>
<p><strong>B. Information Processed as a Data Processor (Customer Data).</strong> When businesses use the Nokvo voice agents, we process data on their behalf, including:</p>
<ul>
  <li><strong>Call Data:</strong> End-user phone numbers, call metadata (duration, timestamps), and call audio recordings.</li>
  <li><strong>Transcripts:</strong> Automated speech-to-text transcripts of conversations between callers and the AI agents.</li>
  <li><strong>Integration Data:</strong> Data synced from third-party CRMs and ad platforms connected by the customer (e.g., Zoho Desk, Google Leads, Meta Leads, WhatsApp, SMS gateways).</li>
  <li><strong>Knowledge Base Information:</strong> Proprietary business documents uploaded to inform the AI agent's responses.</li>
</ul>

<h4>3. Legal Basis for Processing</h4>
<p>Where applicable, we process personal data based on: performance of a contract (to deliver the Nokvo platform and services to our customers); legitimate business interests (to maintain security, troubleshoot issues, and improve platform functionality); compliance with legal obligations; and user consent, where explicitly required by law.</p>

<h4>4. How We Use the Information</h4>
<p>We use Account Data to provide, operate, and maintain the Nokvo platform; process payments and calculate tenant billing/usage costs; send administrative notifications, technical alerts, and security updates; and improve platform robustness, fix bugs, and optimize latency.</p>
<p>We process Customer Data exclusively to facilitate real-time, bidirectional voice conversations using AI; transcribe speech to text, generate LLM responses, and synthesize text to speech; extract conversational outcomes and schedule follow-up actions (e.g., via SMS or CRM sync); and maintain a short-term conversational memory to provide contextually accurate agent responses.</p>

<h4>5. AI Model Training and Data Sales</h4>
<ul>
  <li>Nokvo does not sell, rent, or share Customer Data for advertising purposes.</li>
  <li>Nokvo does not use Customer Data, call recordings, transcripts, uploaded documents, phone numbers, or customer-specific business information to train public foundation models.</li>
  <li>Where permitted by law and customer agreements, Nokvo may use aggregated and de-identified usage metrics, performance statistics, and non-identifiable linguistic patterns to train models in the future and to improve platform performance, speech quality, and multilingual conversational experiences (e.g., to train and refine localized Indian-language voice models). Such information cannot reasonably be used to identify a customer, end-user, or business.</li>
</ul>

<h4>6. Automated Processing</h4>
<p>Certain interactions may be processed automatically by AI systems in real-time. Customers are responsible for reviewing and supervising AI-driven workflows and outcomes where legally required by their respective jurisdictions.</p>

<h4>7. Customer Responsibilities</h4>
<p>As the Data Controller of Customer Data, our customers are responsible for: obtaining any required consent for call recording and AI interactions from end-users; providing the necessary privacy notices to their end-users; ensuring the lawful collection and processing of personal data prior to its input into the Nokvo platform; and configuring data retention settings according to their own legal and compliance obligations.</p>

<h4>8. Children's Privacy</h4>
<p>Nokvo services are intended strictly for business use. Where customers (such as schools or educational institutions) use Nokvo to process data relating to minors, the customer is fully responsible for obtaining all necessary permissions and verifiable parental consents as required by applicable laws (e.g., COPPA, DPDP Act).</p>

<h4>9. Third-Party Subprocessors and Integrations</h4>
<p>To deliver low-latency, enterprise-grade AI voice services, we partner with specialized third-party infrastructure providers:</p>
<ul>
  <li><strong>Cloud Infrastructure &amp; AI Models:</strong> Microsoft Azure (Azure OpenAI, Postgres, Redis, Blob Storage, Key Vault). Data is processed in designated regions (e.g., Central India, Sweden Central, South India).</li>
  <li><strong>Telephony &amp; Messaging:</strong> Plivo, Twilio, and Telnyx for SIP trunking, inbound/outbound voice calls, WhatsApp, and SMS delivery.</li>
  <li><strong>Speech Services (STT/TTS):</strong> Sarvam AI and Soniox for real-time speech-to-text and text-to-speech rendering.</li>
  <li><strong>Vector Databases:</strong> Qdrant for managing embedded knowledge base queries.</li>
  <li><strong>Observability:</strong> LangSmith and OpenTelemetry for prompt tracing and latency diagnostics.</li>
</ul>
<p>Nokvo seeks to maintain appropriate contractual, security, and privacy safeguards with its subprocessors, including Data Processing Agreements where applicable. Nokvo may update, replace, or add subprocessors from time to time. A current list of material subprocessors will be made available upon request or published through our website.</p>

<h4>10. Data Retention</h4>
<p>Call transcripts, call summaries, and recordings are retained according to customer-configured retention settings, subject to platform defaults and legal obligations. Customers remain responsible for selecting appropriate retention settings based on their legal, regulatory, and contractual obligations.</p>
<ul>
  <li><strong>Agent Memory:</strong> High-fidelity, temporary conversational state is held in active memory only for the duration of the session (TTL of 10 minutes post-call). Extracted conversational summaries and transcripts are persisted according to the customer's configured retention settings.</li>
  <li><strong>Account Data:</strong> Retained for as long as your account is active, or as needed to comply with legal and billing obligations.</li>
  <li><strong>Operational Caches:</strong> Transient service lookups (such as the prepaid-balance figures used to gate calling) are held in short-lived caches that expire automatically within seconds and are not separately retained.</li>
</ul>

<h4>11. Data Security and Incident Notification</h4>
<ul>
  <li><strong>Encryption:</strong> Data is encrypted at rest (using Azure Blob Storage and Postgres native encryption) and in transit (via TLS/HTTPS/WSS).</li>
  <li><strong>Key Management:</strong> Sensitive tenant credentials (e.g., telephony keys, OAuth secrets) are managed securely via Azure Key Vault with enforced secret rotation policies.</li>
  <li><strong>Access Controls:</strong> Strict Role-Based Access Control (RBAC), multi-factor authentication (MFA) requirements for super-admins, and robust tenant isolation architectures.</li>
  <li><strong>Security Incidents:</strong> In the event of a confirmed security incident affecting Customer Data, Nokvo will notify affected customers without undue delay and provide relevant information required to comply with applicable laws.</li>
</ul>

<h4>12. Your Privacy Rights</h4>
<p>Depending on your jurisdiction (e.g., EU, UK, California, India), you may have the right to access the personal data we hold about you; correct inaccurate or incomplete data; delete your personal data ("Right to be Forgotten"); restrict or object to certain processing activities; and receive a copy of your data in a structured, machine-readable format (data portability). Nokvo does not sell personal information or Customer Data to third parties.</p>
<p><strong>Note for End-Users:</strong> If you interacted with a Nokvo AI voice agent deployed by one of our customers, please direct your privacy requests directly to that business, as they act as the Data Controller of your information.</p>

<h4>13. Digital Personal Data Protection Act (DPDP Act)</h4>
<p>Nokvo processes personal data in accordance with applicable Indian privacy laws, including the Digital Personal Data Protection Act, 2023, where applicable. We are committed to upholding the principles of data minimization, lawful processing, and security under the DPDP Act.</p>

<h4>14. International Data Transfers</h4>
<p>As a globally available platform, your data may be transferred to, and processed in, countries other than the country in which you are resident. We ensure that such transfers are protected by appropriate safeguards, including Standard Contractual Clauses (SCCs) approved by the European Commission or other legally adequate mechanisms.</p>

<h4>15. Legal Requests and Disclosure</h4>
<p>Nokvo may disclose information where required by law, court order, regulatory authority, or other valid legal process. Where legally permitted, we will provide advance notice to affected customers.</p>

<h4>16. Business Transfers</h4>
<p>If Nokvo is involved in a merger, acquisition, financing, reorganization, or sale of assets, information may be transferred as part of that transaction, subject to applicable confidentiality obligations.</p>

<h4>17. Changes to This Privacy Policy</h4>
<p>We may update this Privacy Policy from time to time to reflect changes in our platform infrastructure, new third-party integrations, or legal requirements. We will notify you of significant changes by emailing the primary contact on your account or displaying a prominent notice within the Nokvo One portal.</p>

<h4>18. Contact &amp; Grievance Officer</h4>
<p>For any questions, privacy requests, or grievances regarding this Privacy Policy or your personal data, please contact NEEDLES COUTURE AND COLLECTIVE PRIVATE LIMITED at <a href="mailto:officialnokvo@nokvo.org">officialnokvo@nokvo.org</a>. In accordance with the DPDP Act, you may address grievances to our designated point of contact at the same email; we will respond within the timelines required by applicable law.</p>
`;
