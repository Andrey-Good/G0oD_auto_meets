// Injected into a matching meeting tab when local chat capture is active.
if (!globalThis.__goodAutoMeetsChatInstalled) {
  globalThis.__goodAutoMeetsChatInstalled = true;
  let observer = null;
  let timer = null;
  let currentSelector = null;
  let lastText = new WeakMap();

  function stop() {
    observer?.disconnect();
    clearInterval(timer);
    observer = null;
    timer = null;
    currentSelector = null;
    chrome.runtime.sendMessage({type: 'stopped'});
  }

  chrome.runtime.onMessage.addListener((message, _sender, reply) => {
    if (message?.type !== 'start') return;
    try {
      if (observer && currentSelector === message.options.selector) {
        reply({ok: true});
        return;
      }
      stop();
      const selector = message.options.selector;
      document.querySelector(selector); // Reject an invalid selector immediately.
      lastText = new WeakMap();
      let pending = false;
      const scan = () => {
        pending = false;
        for (const node of document.querySelectorAll(selector)) {
          const text = (node.innerText || node.textContent || '').trim();
          if (!text || lastText.get(node) === text) continue;
          lastText.set(node, text);
          chrome.runtime.sendMessage({type: 'message', text}, (answer) => {
            if (chrome.runtime.lastError || answer?.stop) stop();
          });
        }
      };
      observer = new MutationObserver(() => {
        if (!pending) {
          pending = true;
          setTimeout(scan, 100);
        }
      });
      observer.observe(document.documentElement, {subtree: true, childList: true, characterData: true});
      currentSelector = selector;
      scan();
      timer = setInterval(() => chrome.runtime.sendMessage({type: 'heartbeat'}, (answer) => {
        if (chrome.runtime.lastError || answer?.stop) stop();
      }), 5000);
      reply({ok: true});
    } catch (error) {
      stop();
      reply({ok: false, error: String(error)});
    }
  });
}
