import json
import hmac
import hashlib
import time
from urllib.parse import urlencode
import asyncio
import aiohttp
import async_timeout


# Load credentials from TK_credential.json
with open('TK_credential.json') as f:
    credentials = json.load(f)

app_key = credentials['app_key']
app_secret = credentials['app_secret']
access_token = credentials['access_token']
refresh_token = credentials['refresh_token']
shop_cipher = credentials['shop_cipher']

product_id = "1729406908937704065"
sku_id = "1729406908937769601"
warehouse_id = "7277687726564476715"

def generate_sha256_signature(secret, message):
    """Generate SHA256 HMAC signature."""
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()

def get_tiktok_api_signature(app_secret, url_path, query_params, body=''):
    """Generate the signature for TikTok API request."""
    query_params = {k: v for k, v in query_params.items() if k not in ['sign', 'access_token']}
    sorted_keys = sorted(query_params.keys())
    concatenated_params = ''.join(f'{key}{query_params[key]}' for key in sorted_keys)
    input_string = url_path + concatenated_params
    if body:
        input_string += body
    input_string = app_secret + input_string + app_secret
    return generate_sha256_signature(app_secret, input_string)

async def update_product():
    url_path = f'/product/202309/products/{product_id}/partial_edit'
    query_params = {
        'app_key': app_key,
        'timestamp': str(int(time.time())),
        'shop_cipher': shop_cipher
    }
    body = json.dumps({
        "package_dimensions": {
            "height": "12",
            "length": "10",
            "unit": "CENTIMETER",
            "width": "11"
        },
        "product_attributes": [
            {
                "id": "100392",
                "values": [
                    {
                        "id": "1001533",
                        "name": "Birthday"
                    }
                ]
            }
        ],
        "size_chart": {
            "image": {
                "uri": "tos-maliva-i-o3syd03w52-us/c668cdf70b7f483c94dbe"
            },
            "template": {
                "id": "7267563252536723205"
            }
        },
        "skus": [
            {
                "external_sku_id": sku_id,
                "id": sku_id,
                "identifier_code": {
                    "code": "12345678901234",
                    "type": "GTIN"
                },
                "inventory": [
                    {
                        "quantity": 999,
                        "warehouse_id": warehouse_id
                    }
                ],
                "price": {
                    "amount": "100.00",
                    "currency": "USD"
                },
                "sales_attributes": [
                    {
                        "id": "100089",
                        "name": "Specification",
                        "sku_img": {
                            "uri": "tos-maliva-i-o3syd03w52-us/c668cdf70b7f483c94dbe"
                        },
                        "value_id": "1729592969712207000",
                        "value_name": "XL"
                    }
                ],
                "seller_sku": "seller sku xxaa",
                "sku_unit_count": "100.00"
            }
        ],
        "title": "Title-xxaa",
        "video": {
            "id": "v09e40f40000cfu0ovhc77ub7fl97k4w"
        }
    })

    signature = get_tiktok_api_signature(app_secret, url_path, query_params, body)
    query_params['sign'] = signature
    query_params['access_token'] = access_token

    async with aiohttp.ClientSession() as session:
        with async_timeout.timeout(10):
            async with session.post(
                f'https://open-api.tiktokglobalshop.com{url_path}',
                params=query_params,
                headers={'content-type': 'application/json'},
                data=body
            ) as response:
                result = await response.json()
                print(result)

# Run the update_product function
asyncio.run(update_product())
