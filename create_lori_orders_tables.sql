-- Create the lori_orders table to store PDF order information
CREATE TABLE IF NOT EXISTS lori_orders (
    po_number VARCHAR(50) PRIMARY KEY,
    batch_date DATE NOT NULL,
    email_date DATETIME,
    order_date VARCHAR(50),
    delivery_date VARCHAR(50),
    cancel_date VARCHAR(50),
    ship_to_address TEXT,
    total_qty DECIMAL(10,2),
    total_cost DECIMAL(12,2),
    status ENUM('pending', 'processed', 'shipped', 'completed') DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_batch_date (batch_date),
    INDEX idx_status (status)
);

-- Create the lori_order_items table to store individual items within each order
CREATE TABLE IF NOT EXISTS lori_order_items (
    id INT AUTO_INCREMENT PRIMARY KEY,
    po_number VARCHAR(50) NOT NULL,
    vendor_item VARCHAR(100),
    color VARCHAR(50),
    quantity DECIMAL(10,2),
    description TEXT,
    unit_price DECIMAL(10,2),
    extended_cost DECIMAL(12,2),
    upc VARCHAR(20),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (po_number) REFERENCES lori_orders(po_number) ON DELETE CASCADE,
    INDEX idx_po_number (po_number),
    INDEX idx_vendor_item (vendor_item)
);