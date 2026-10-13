# Data dictionary

All data is **synthetic**. Every investor, folio, account, distributor, bank and AMC is fictional. Extracts were taken on **9 October 2026**. Amounts are in Indian rupees. PANs and account numbers are partially masked, as in the source screens.

## data/kra_kyc_snapshot.csv: KRA lookup, one row per PAN

| Column | Meaning |
|---|---|
| pan_masked | PAN with two digits masked. The 4th character is P for individuals and C for companies |
| name_as_per_kra | Name as the KRA holds it |
| dob | Date of birth, or date of incorporation |
| kra_name | KRA holding the record: CVL, NDML, DOTEX, CAMS, KFIN |
| kra_status_code | Three-digit code. The first digit is the KRA (0 CVL, 1 NDML, 2 DOTEX, 3 CAMS, 5 KFIN). The last two digits are the status: 07 KYC Validated, 02 KYC Registered, 03 On Hold, 04 KYC Rejected, 01 Under Process, 05 Not Available, 99 KRA web service not reachable |
| kra_status | Status text as returned by the KRA |
| email_validated, mobile_validated | Y/N, per the KRA |
| pan_aadhaar_linked | Y / N / NA (not applicable for non-individuals) |
| last_checked_at | Time of the lookup (YYYY-MM-DD HH:MM, IST) |

## data/folio_master.csv: one row per folio

| Column | Meaning |
|---|---|
| folio_no | Eight digits, then `/`, then two check digits. NMF folios start 401, TMF folios start 711 |
| amc_code | NMF or TMF |
| holding_mode | SI single, JO joint, AS anyone-or-survivor |
| tax_status | `01 Individual`, `02 On behalf of minor`, `03 HUF`, `21 NRI-NRE`, `24 NRI-NRO` |
| first_holder_name / first_holder_pan_masked | First-named holder |
| second_holder_name / second_holder_pan_masked | Second holder, if any |
| guardian_name / guardian_pan_masked / guardian_relation | For minor folios: FATHER, MOTHER or COURT_APPOINTED |
| minor_dob | Minor's date of birth (minor folios only) |
| registered_email_masked / registered_mobile_masked | Current registered contact details |
| contact_last_changed_on | Date the registered email or mobile last changed (blank if never) |
| fatca_status | SUBMITTED / NOT_SUBMITTED |
| nomination | REGISTERED / OPTED_OUT / NOT_RECORDED |
| folio_flag | NONE or STOP_LOCK |
| opened_on | Folio opening date |

## data/bank_mandates.csv: one row per folio and bank account

| Column | Meaning |
|---|---|
| folio_no | Folio |
| account_no_masked | Account number; last four digits visible |
| ifsc / bank_name | Branch IFSC and bank |
| account_type | SB savings, CB current, NE NRE, NO NRO |
| account_holders | Holder names as validated with the bank, separated by `;` |
| is_default | Y for the folio's default payout account |
| status | VALIDATED, PENDING_VALIDATION, REJECTED, DELETED |
| validation_method | CANCELLED_CHEQUE, PENNY_DROP, NPCI_PAN, BANK_LETTER |
| registered_on | Date the account became registered (blank while pending) |
| cob_request_received_on | For accounts added by a change-of-bank request: the date the request was received |

## data/transactions.csv: transaction register, about 12 months of history plus pending items

| Column | Meaning |
|---|---|
| txn_id | Register ID |
| folio_no, amc_code | Folio and AMC |
| scheme_code, scheme_name, plan, option | Scheme; REGULAR or DIRECT; GROWTH, IDCW_PAYOUT or IDCW_REINVEST |
| txn_type | NEW fresh purchase, ADD additional purchase, SIP_REG SIP registration with first instalment, SIP instalment, RED redemption, SWI/SWO switch in/out |
| amount_inr | Amount in rupees |
| units, nav | Units allotted and NAV applied (processed purchases and redemptions) |
| txn_date | Transaction date |
| payment_mode | NEFT, RTGS, IMPS, UPI, NETBANKING, NACH, CHEQUE, DD, CASH |
| remitter_account_masked, remitter_name | Paying account and name, as received from the bank or payment gateway |
| utr | Bank or UPI reference |
| arn, euin | Distributor codes quoted on the transaction (blank for Direct) |
| status | PROCESSED, PENDING_TCC (waiting for the desk), REJECTED, REVERSED |
| tcc_reason_code | Reason code recorded by the desk on past outcomes |

## data/distributor_master.csv: one row per ARN and EUIN

| Column | Meaning |
|---|---|
| arn, distributor_name | Distributor |
| arn_valid_till, arn_status | Validity and status: ACTIVE, EXPIRED, SUSPENDED, CANCELLED |
| euin, euin_holder_name, euin_valid_till | Salesperson code |
| empanelled_nmf, empanelled_tmf | Y/N: empanelled with the AMC |

## policies/

| File | What it is |
|---|---|
| Validus_TCC_SOP_v4.1.docx | TCC Standard Operating Procedure, version 4.1 |
| Validus_TCC_SOP_v3.2.pdf | TCC Standard Operating Procedure, version 3.2 |
| Tungabhadra_MF_Client_Addendum_TMF-ADD-02.pdf | Tungabhadra Mutual Fund client addendum |

## inbox/

| File | What it is |
|---|---|
| R01.docx … R10.docx | Ten requests as received in the desk mailbox, printed from the mail client |
| attachments/ | The files attached to those emails |
| requests.json | The same ten requests in the API request shape, with attachments base64-encoded |
