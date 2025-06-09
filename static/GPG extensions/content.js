let popupCreated = false;

setTimeout(() => {
  if (popupCreated) return;

  if (window.location.href.includes("admin.genuinepartsgiant.com/popup/HPD/orderDetails/orders") || 
      window.location.href.includes("admin.genuinepartsgiant.com/popup/HPN/orderDetails/orders")||
	    window.location.href.includes("admin.genuinepartsgiant.com/popup/TPD/orderDetails/orders")||
      window.location.href.includes("admin.genuinepartsgiant.com/popup/APW/orderDetails/orders")) {
    
    const salesOrderNoElement = document.evaluate(
      '//strong[contains(text(), "Sales Order No.")]',
      document,
      null,
      XPathResult.FIRST_ORDERED_NODE_TYPE,
      null
    ).singleNodeValue;
    const orderNumber = salesOrderNoElement.textContent.replace("Sales Order No.:", "").trim();
    navigator.clipboard.writeText(orderNumber)
        .then(() => {
            console.log('Order number copied:', orderNumber);
        })
        .catch(err => {
            console.log('Failed to copy:', err);
        });
    const orderDateElement = document.evaluate(
      '//strong[contains(text(), "Order Date")]',
      document,
      null,
      XPathResult.FIRST_ORDERED_NODE_TYPE,
      null
    ).singleNodeValue;

    const invoiceData = {
      orderNumber: salesOrderNoElement
          ? salesOrderNoElement.textContent.replace("Sales Order No.:", "").trim()
          : "N/A",
      orderDate: orderDateElement
          ? orderDateElement.textContent.replace("Order Date:", "").trim()
          : "N/A",
      lineItems: [],
  };

    const partRows = document.evaluate(
      '//table[contains(@class, "order-item-list-table")]//tr',
      document,
      null,
      XPathResult.ORDERED_NODE_SNAPSHOT_TYPE,
      null
    );

    for (let i = 0; i < partRows.snapshotLength; i++) {
      const row = partRows.snapshotItem(i);
      if (!row.textContent.trim() || row.textContent.match(/Subtotal|Tax|Total|Best Standard Delivery/i)) continue;

      const lineNumber = document.evaluate('./td[1]', row, null, XPathResult.STRING_TYPE, null).stringValue.trim();
      const partNumber = document.evaluate('./td[2]/div/span[1]', row, null, XPathResult.STRING_TYPE, null).stringValue.trim();
      const altpartNumber = document.evaluate('./td[2]/div/span[2]', row, null, XPathResult.STRING_TYPE, null).stringValue.trim();
      const qty = document.evaluate('./td[4]', row, null, XPathResult.STRING_TYPE, null).stringValue.trim();
      const gpgiPrice = document.evaluate('./td[8]', row, null, XPathResult.STRING_TYPE, null).stringValue.trim();
      const partLink = document.evaluate(
        './/span[@class="order-item-list-links"]/a',
        row,
        null,
        XPathResult.FIRST_ORDERED_NODE_TYPE,
        null
      ).singleNodeValue?.href || "";
      if (/^\d+$/.test(lineNumber) && Number(lineNumber) >= 1 && Number(lineNumber) <= 12) {
        invoiceData.lineItems.push({
          lineNumber,
          partNumber,
          altpartNumber,
          qty,
          price: gpgiPrice,
          partLink,
        });
      }
    }

    createPopup(invoiceData);
    popupCreated = true;
  }
}, 3000);


function createPopup(data) {
  const popupContainer = document.createElement("div");
  Object.assign(popupContainer.style, {
    position: "fixed",
    top: "10%",
    left: "10%",
    width: "80%",
    height: "80%",
    backgroundColor: "white",
    boxShadow: "0 0 10px rgba(0, 0, 0, 0.5)",
    padding: "20px",
    overflow: "auto",
    zIndex: "9999"
});

  // Add drag header
  const dragHeader = document.createElement("div");
  Object.assign(dragHeader.style, {
    padding: "10px",
    marginBottom: "10px",
    backgroundColor: "#f0f0f0",
    cursor: "move"
});
  dragHeader.textContent = "Drag here to move window";

  // Drag functionality
  let isDragging = false;
  let currentX;
  let currentY;
  let initialX;
  let initialY;

  dragHeader.addEventListener("mousedown", (e) => {
    isDragging = true;
    initialX = e.clientX - popupContainer.offsetLeft;
    initialY = e.clientY - popupContainer.offsetTop;
  });

  document.addEventListener("mousemove", (e) => {
    if (isDragging) {
      e.preventDefault();
      currentX = e.clientX - initialX;
      currentY = e.clientY - initialY;
      
      popupContainer.style.left = `${currentX}px`;
      popupContainer.style.top = `${currentY}px`;
    }
  });

  document.addEventListener("mouseup", () => {
    isDragging = false;
  });





  // Rest of your existing popup content
  const closeButton = document.createElement("button");
  closeButton.textContent = "Close";
  Object.assign(closeButton.style, {
    float: "right",
    marginBottom: "20px",
    fontSize: "24px",
    padding: "12px 24px",
    backgroundColor: "#ff4444",
    color: "white",
    border: "none",
    borderRadius: "4px",
    cursor: "pointer"
});
  closeButton.onclick = () => popupContainer.remove();



  const createInvoiceButton = document.createElement("button");
  createInvoiceButton.textContent = "Save Invoice";
  Object.assign(createInvoiceButton.style, {
      float: "right",
      marginRight: "10px",
      marginBottom: "20px",
      backgroundColor: "#4CAF50",
      color: "white",
      border: "none",
      padding: "12px 24px",
      borderRadius: "4px",
      cursor: "pointer",
      fontSize: "24px"
  });
  const buttonContainer = document.createElement('div');
  Object.assign(buttonContainer.style, {
      position: 'absolute',  // Changed from 'fixed'
      bottom: '20px',       // Use pixels instead of percentage
      right: '20px',
      display: 'flex',
      gap: '20px',
      zIndex: '10000'
  });
  // Now we can safely add buttons to container
  buttonContainer.appendChild(createInvoiceButton);
  buttonContainer.appendChild(closeButton);
  createInvoiceButton.addEventListener("click", () => {
    if (!data) {
        console.error("Data is undefined");
        alert("No data available to save.");
        return; // Exit the function early
    }

    const messageData = {
        type: 'SAVE_ORDER',
        data: {
            orderDate: data.orderDate,
            orderNumber: data.orderNumber,
            lineItems: data.lineItems.map(item => ({
                lineNumber: item.lineNumber,
                partNumber: item.partNumber,
                qty: item.qty,
                price: item.price
            }))
        }
    };

    window.chrome.runtime.sendMessage(messageData, response => {
      if (response && response.success) {
        alert(response.data.message); // ← access the message inside `data`
    } else {
        alert("Failed to save order data: " + (response?.error || "No response received."));
    }
    });
});
console.log("Data before sending:", data);


// Calculate total price from line items
const totalPrice = data.lineItems.reduce((sum, item) => {
  return sum + (Number(item.qty) * Number(item.price.replace('$', '')));
}, 0);

const invoiceDetails = document.createElement("div");
Object.assign(invoiceDetails.style, {
    display: "grid",
    gridTemplateColumns: "1fr 1fr 1fr", // Changed to 3 columns
    gap: "20px",
    marginBottom: "30px",
    fontSize: "24px"
});


invoiceDetails.innerHTML = `
    <div>
        <strong>Order Number:</strong><br>
        ${data.orderNumber}
        <span style="
            cursor: pointer;
            margin-left: 10px;
            padding: 5px;
            background: #f0f0f0;
            border-radius: 4px;
        " onclick="navigator.clipboard.writeText('${data.orderNumber}')">
            📋
        </span>
    </div>
    <div><strong>Order Date:</strong><br>${data.orderDate}</div>
    <div><strong>Vendor Invoice Total:</strong><br>$${totalPrice.toFixed(2)}</div>
`;

const lineItems = document.createElement("div"); // Changed from ul to div for grid
Object.assign(lineItems.style, {
    display: "grid",
    gridTemplateColumns: "repeat(5, 1fr)",
    gap: "15px",
    padding: "20px",
    backgroundColor: "#f5f5f5",
    borderRadius: "8px"
});

// Create headers
const headers = `
    <div class="header"><strong>Line</strong></div>
    <div class="header"><strong>Part Number</strong></div>
    <div class="header"><strong>Alt Part</strong></div>
    <div class="header"><strong>Quantity</strong></div>
    <div class="header"><strong>Price</strong></div>
`;
lineItems.innerHTML = headers;

// Style headers
const headerElements = lineItems.querySelectorAll('.header');
headerElements.forEach(header => {
    Object.assign(header.style, {
        fontSize: "20px",
        padding: "10px",
        backgroundColor: "#4CAF50",
        color: "white",
        borderRadius: "4px"
    });
});

// Add data rows
data.lineItems.forEach((item) => {
  const rowData = `
      <div>${item.lineNumber}</div>
      <div><a href="${item.partLink}" target="_blank" style="color: #0066cc; text-decoration: none;">${item.partNumber}</a></div>
      <div>${item.altpartNumber}</a></div>
      <div>${item.qty}</div>
      <div>${item.price}</div>
  `;
  const rowContainer = document.createElement('div');
  rowContainer.style.display = "contents";
  rowContainer.innerHTML = rowData;
  lineItems.appendChild(rowContainer);
});

// Add part links separately
const linkContainer = document.createElement('div');
Object.assign(linkContainer.style, {
    marginTop: "20px",
    display: "grid",
    gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))",
    gap: "10px"
});

const copyIcon = invoiceDetails.querySelector('span');
copyIcon.onclick = () => {
    navigator.clipboard.writeText(data.orderNumber);
    copyIcon.textContent = '✓';  // Change to checkmark
    setTimeout(() => {
        copyIcon.textContent = '📋';  // Revert back after 1 second
    }, 1000);
};

popupContainer.append(dragHeader, invoiceDetails, lineItems, linkContainer,buttonContainer);
document.body.appendChild(popupContainer);
}