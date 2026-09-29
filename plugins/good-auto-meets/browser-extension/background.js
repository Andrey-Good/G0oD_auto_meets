const ENDPOINT = 'http://127.0.0.1:18765';

async function config() {
  const response = await fetch(`${ENDPOINT}/config`, {
    cache: 'no-store', headers: {'X-Extension-Version': chrome.runtime.getManifest().version}
  });
  if (!response.ok) throw new Error(`Collector returned ${response.status}`);
  return response.json();
}

function matches(page, target) {
  const actual = new URL(page);
  const wanted = new URL(target);
  return actual.origin === wanted.origin && actual.pathname.startsWith(wanted.pathname);
}

async function diagnostic(options, page, error = null) {
  await fetch(`${ENDPOINT}/diagnostic`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json', Authorization: `Bearer ${options.token}`},
    body: JSON.stringify({url: page, error: error ? String(error) : null})
  });
}

async function activate(tab, options) {
  if (!tab.id || !tab.url || !options.active || !matches(tab.url, options.url)) return false;
  await chrome.scripting.executeScript({target: {tabId: tab.id}, files: ['observer.js']});
  const answer = await chrome.tabs.sendMessage(tab.id, {type: 'start', options});
  if (!answer?.ok) throw new Error(answer?.error || 'Observer did not start');
  await chrome.action.setBadgeText({tabId: tab.id, text: 'ON'});
  await chrome.action.setBadgeBackgroundColor({tabId: tab.id, color: '#16803c'});
  return true;
}

async function scanTabs() {
  try {
    const options = await config();
    if (!options.active) return;
    for (const tab of await chrome.tabs.query({})) {
      if (tab.url && matches(tab.url, options.url)) {
        try {
          await activate(tab, options);
          await diagnostic(options, tab.url);
        } catch (error) {
          await diagnostic(options, tab.url, error);
          console.error('Chat tab:', error);
        }
      }
    }
  } catch (_) {
    // A collector is normally absent outside meetings.
  }
}

chrome.alarms.create('auto-meets-chat', {periodInMinutes: 0.5});
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === 'auto-meets-chat') scanTabs();
});
chrome.runtime.onStartup.addListener(scanTabs);

chrome.action.onClicked.addListener(async (tab) => {
  if (!tab.id || !tab.url) return;
  try {
    const options = await config();
    if (!await activate(tab, options)) throw new Error('Wrong meeting tab');
    await diagnostic(options, tab.url);
  } catch (error) {
    await chrome.action.setBadgeText({tabId: tab.id, text: '!'});
    await chrome.action.setBadgeBackgroundColor({tabId: tab.id, color: '#b42318'});
    console.error('Good Auto Meets Chat:', error);
  }
});

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (message?.type === 'stopped' && sender.tab?.id) {
    chrome.action.setBadgeText({tabId: sender.tab.id, text: ''});
    return;
  }
  if (!sender.tab?.id || !['message', 'heartbeat'].includes(message?.type)) return;
  (async () => {
    try {
      const options = await config();
      if (!options.active || !matches(sender.tab.url, options.url)) {
        reply({ok: false, stop: true});
        return;
      }
      if (message.type === 'heartbeat') {
        await diagnostic(options, sender.tab.url);
        reply({ok: true});
        return;
      }
      const response = await fetch(`${ENDPOINT}/messages`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json', Authorization: `Bearer ${options.token}`},
        body: JSON.stringify({text: message.text, url: sender.tab.url})
      });
      reply({ok: response.ok, stop: response.status === 410});
    } catch (error) {
      reply({ok: false, stop: true});
    }
  })();
  return true;
});
