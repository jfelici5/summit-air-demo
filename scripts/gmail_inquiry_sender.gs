// Deploy as a Web app from helpdesk.summitair@gmail.com, executing as that account.
// Allow web access; requests authenticate with the token below.
// Set INQUIRY_EMAIL_TOKEN in Project Settings > Script properties.
const EXPECTED_SENDER = 'helpdesk.summitair@gmail.com';

function jsonResult(value) {
  return ContentService.createTextOutput(JSON.stringify(value))
    .setMimeType(ContentService.MimeType.JSON);
}

function doGet() {
  return jsonResult({service: 'Summit Air inquiry acknowledgments'});
}

function doPost(event) {
  let input;
  try {
    if (!event.postData || event.postData.contents.length > 5000) {
      return jsonResult({email_status: 'failed'});
    }
    input = JSON.parse(event.postData.contents);
  } catch (_) {
    return jsonResult({email_status: 'failed'});
  }
  const properties = PropertiesService.getScriptProperties();
  const secret = properties.getProperty('INQUIRY_EMAIL_TOKEN');
  if (!secret || !input || typeof input.token !== 'string' || input.token !== secret) {
    return jsonResult({email_status: 'unauthorized'});
  }
  if (Session.getEffectiveUser().getEmail().toLowerCase() !== EXPECTED_SENDER) {
    return jsonResult({email_status: 'failed', reason: 'wrong_sender_account'});
  }
  if (typeof input.request_id !== 'string' ||
      !/^[a-f0-9-]{36}$/.test(input.request_id) ||
      typeof input.recipient !== 'string' || input.recipient.length > 320 ||
      !/^[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+$/.test(input.recipient)) {
    return jsonResult({email_status: 'failed'});
  }

  const lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) {
    return jsonResult({email_status: 'unknown'});
  }
  const key = 'ack_' + input.request_id;
  try {
    const previous = properties.getProperty(key);
    if (previous) {
      return jsonResult({email_status: JSON.parse(previous).status});
    }
    if (MailApp.getRemainingDailyQuota() < 1) {
      return jsonResult({email_status: 'failed', reason: 'daily_quota'});
    }
    // Persist the claim before sending. Never resend an uncertain attempt.
    properties.setProperty(key, JSON.stringify({status: 'unknown', at: Date.now()}));
    const firstName = typeof input.first_name === 'string'
      ? input.first_name.replace(/[\r\n]/g, ' ').slice(0, 80).trim() || 'there'
      : 'there';
    MailApp.sendEmail({
      to: input.recipient,
      subject: 'Summit Air — we received your inquiry',
      name: 'Summit Air',
      replyTo: EXPECTED_SENDER,
      body: 'Thanks, ' + firstName + ', for calling Summit Air today!\n\n' +
        "We've received your inquiry. Our team will review it and follow up with you.\n\n" +
        'You can reply to this email with any additional information.\n\n' +
        'This confirms your inquiry was received; no service visit has been booked.\n\n' +
        'Summit Air\n' + EXPECTED_SENDER
    });
    properties.setProperty(key, JSON.stringify({status: 'accepted', at: Date.now()}));
    return jsonResult({email_status: 'accepted'});
  } catch (_) {
    // A send may have succeeded; disclose uncertainty and leave the claim intact.
    return jsonResult({email_status: 'unknown'});
  } finally {
    lock.releaseLock();
  }
}
