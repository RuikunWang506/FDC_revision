chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === "complete" && tab.url && 
      (tab.url.includes("admin.genuinepartsgiant.com/popup/HPD/orderDetails/orders") ||
       tab.url.includes("admin.genuinepartsgiant.com/popup/HPN/orderDetails/orders") ||
	   tab.url.includes("admin.genuinepartsgiant.com/popup/TPD/orderDetails/orders") ||

       tab.url.includes("admin.genuinepartsgiant.com/popup/APW/orderDetails/orders")) 
  ) {
    chrome.scripting.executeScript({
      target: { tabId: tabId },
      files: ["content.js"]
    }).catch(error => console.error("Script injection failed:", error));
  }
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === 'SAVE_ORDER') {
    fetch('https://www.fdcef.com/gpginvoice', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      },
      body: JSON.stringify(message.data)
    })
    .then(response => {
      if (!response.ok) {
        throw new Error(`Server responded with status: ${response.status}`);
      }
      return response.json();
    })
    .then(data => sendResponse({success: true, data}))
    .catch(error => {
      console.error("API request failed:", error);
      sendResponse({success: false, error: error.toString()});
    });
    return true; // Keep the message channel open for the async response
  }
  // Return false for unhandled message types
  return false;
});
