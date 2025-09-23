
@lori_automation_bp.route("/lori_automation", methods=["GET", "POST"])
def lori_automation():
    if 'username' not in session:
        return redirect(url_for("auth.login"))
    
    if request.method == "POST":
        if 'files[]' not in request.files:
            return jsonify({'error': 'No files uploaded'}), 400
        
        files = request.files.getlist('files[]')
        excel_files = []
        po_numbers = []
        addresses = []
        pdf_merger = PdfMerger()
        
        for file in files:
            if file.filename.endswith('.pdf'):
                pdf_content = io.BytesIO(file.read())
                result = process_pdf(pdf_content)
                
                excel_files.append({
                    'content': result['excel'].getvalue(),
                    'filename': result['filename']
                })
                po_numbers.append(result['po_number'])
                addresses.append(result['address'])
                
                pdf_content.seek(0)
                pdf_merger.append(pdf_content)
        
        # Create ZIP of Excel files
        memory_zip = io.BytesIO()
        with zipfile.ZipFile(memory_zip, 'w') as zf:
            for excel in excel_files:
                zf.writestr(excel['filename'], excel['content'])
        
        # Create combined PDF
        pdf_output = io.BytesIO()
        pdf_merger.write(pdf_output)
        pdf_output.seek(0)
        
        # Prepare ShipStation data
        shipstation_data = []
        for po, addr in zip(po_numbers, addresses):
            addr_parts = addr.split('\n')
            current_time = datetime.now().isoformat()
            
            shipstation_data.append({
                "orderNumber": po,
                "orderDate": current_time,
                "paymentDate": current_time,
                "shipByDate": current_time,
                "orderStatus": "awaiting_shipment",
                "customerUsername": "",
                "customerEmail": "",
                "billTo": {
                    "name": f"Lori's Gift Shop #{po[:4]}"
                },
                "shipTo": {
                    "name": f"Lori's Gift Shop #{po[:4]}",
                    "company": addr_parts[0] if len(addr_parts) > 0 else "",
                    "street1": addr_parts[1] if len(addr_parts) > 1 else "",
                    "street2": "",
                    "street3": "",
                    "city": addr_parts[2].split(',')[0] if len(addr_parts) > 2 else "",
                    "state": addr_parts[2].split(',')[1].split()[0] if len(addr_parts) > 2 else "",
                    "postalCode": addr_parts[2].split(',')[1].split()[1] if len(addr_parts) > 2 else "",
                    "country": "US",
                    "phone": "",
                    "residential": "false"
                },
                "carrierCode": "ups",
                "serviceCode": "ups_ground",
                "packageCode": "package",
                "confirmation": "none",
                "shipDate": datetime.now().strftime("%Y-%m-%d"),
                "insuranceOptions": {
                    "provider": "carrier",
                    "insureShipment": "true",
                    "insuredValue": "0"
                },
                "advancedOptions": {
                    "nonMachinable": "false",
                    "saturdayDelivery": "false",
                    "containsAlcohol": "false",
                    "mergedOrSplit": "false",
                    "customField1": "",
                    "customField2": "",
                    "source": "Webstore",
                    "billToParty": "third_party",
                    "billToAccount": "7504E1",
                    "billToPostalCode": "75006",
                    "billToCountryCode": "US"
                }
            })

        # Prepare individual Excel files data for frontend
        individual_excel_files = []
        for excel in excel_files:
            individual_excel_files.append({
                'filename': excel['filename'],
                'content': base64.b64encode(excel['content']).decode('utf-8')
            })

        return jsonify({
            'excel_zip': base64.b64encode(memory_zip.getvalue()).decode('utf-8'),
            'combined_pdf': base64.b64encode(pdf_output.getvalue()).decode('utf-8'),
            'shipstation_data': shipstation_data,
            'individual_excel_files': individual_excel_files
        })
    return render_template("lori_automation.html", current_user=session['username'])

@lori_automation_bp.route("/send_to_shipstation", methods=["POST"])
def send_to_shipstation():
    data = request.get_json()
    url = "https://ssapi.shipstation.com/orders/createorders"
    headers = {'Content-Type': 'application/json'}
    
    # Get all orders
    all_orders = data['shipstation_data']
    
    # Ensure each order has the required fields
    for order in all_orders:
        order['orderStatus'] = 'awaiting_shipment'  # Required field
        order['billTo'] = {
            "name": f"Lori's Gift Shop #{order['orderNumber'][:4]}",
            "company": order['shipTo']['company'],
            "street1": order['shipTo']['street1'],
            "city": order['shipTo']['city'],
            "state": order['shipTo']['state'],
            "postalCode": order['shipTo']['postalCode'],
            "country": "US",
            "phone": "",
        }
    
    # Batch orders in groups of 80
    batch_size = 80
    batches = [all_orders[i:i + batch_size] for i in range(0, len(all_orders), batch_size)]
    
    # Process each batch
    all_responses = []
    for batch_index, batch in enumerate(batches):
        current_app.logger.info(f"Processing batch {batch_index + 1}/{len(batches)} with {len(batch)} orders")
        
        response = requests.post(
            url, 
            headers=headers, 
            json=batch, 
            auth=(current_app.config['APIKEY'], current_app.config['APIPASS'])
        )
        
        # Log the response status
        current_app.logger.info(f"Batch {batch_index + 1} response status: {response.status_code}")
        
        if response.status_code != 200:
            current_app.logger.error(f"ShipStation API error for batch {batch_index + 1}: Status {response.status_code}, Response: {response.text}")
        
        try:
            response_data = response.json()
            all_responses.append(response_data)
        except Exception as e:
            current_app.logger.error(f"Error parsing response for batch {batch_index + 1}: Status {response.status_code}, Error: {str(e)}, Response text: {response.text[:500]}")
            all_responses.append({
                "error": str(e),
                "status_code": response.status_code,
                "response_text": response.text[:500]
            })
    
    # Combine all responses
    combined_response = {
        "batches_processed": len(batches),
        "total_orders": len(all_orders),
        "batch_responses": all_responses
    }
    
    return jsonify(combined_response)
