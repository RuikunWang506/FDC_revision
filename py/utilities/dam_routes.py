from flask import request, jsonify, session, send_file
import os
from py.utilities.setting import Config, resize_image, login_required
import py.utilities.models


def register_dam_routes(app):
    """Register Digital Asset Management (DAM) routes with the Flask app."""

    @app.route("/assets")
    # @login_required  # Temporarily disabled for testing
    def list_assets():
        """List all active images for a SKU."""
        sku = request.args.get('sku', '').upper().strip()
        current_user = session['username']

        if not sku:
            return jsonify({'error': 'SKU parameter required'}), 400

        try:
            db = py.utilities.models.SessionLocal()
            assets = db.query(py.utilities.models.ImageAsset).filter_by(
                sku=sku,
                is_active=True
            ).order_by(py.utilities.models.ImageAsset.version).all()

            asset_list = []
            for asset in assets:
                asset_list.append({
                    'id': asset.id,
                    'sku': asset.sku,
                    'version': asset.version,
                    'filename': asset.filename,
                    'source': asset.source,
                    'width': asset.width,
                    'height': asset.height,
                    'is_canonical': asset.is_canonical,
                    'created_at': asset.created_at.isoformat() if asset.created_at else None
                })

            return jsonify({'sku': sku, 'assets': asset_list})

        except Exception as e:
            app.logger.error(f"Error listing assets for SKU {sku}: {e}")
            return jsonify({'error': 'Internal server error'}), 500
        finally:
            db.close()

    @app.route("/assets/canonical")
    @login_required
    def get_canonical_asset():
        """Return the canonical (default) image for a SKU."""
        sku = request.args.get('sku', '').upper().strip()
        current_user = session['username']

        if not sku:
            return jsonify({'error': 'SKU parameter required'}), 400

        try:
            db = py.utilities.models.SessionLocal()
            # First try to get explicitly marked canonical
            asset = db.query(py.utilities.models.ImageAsset).filter_by(
                sku=sku,
                is_canonical=True,
                is_active=True
            ).first()

            # If no canonical marked, get the first version
            if not asset:
                asset = db.query(py.utilities.models.ImageAsset).filter_by(
                    sku=sku,
                    is_active=True
                ).order_by(py.utilities.models.ImageAsset.version).first()

            if not asset:
                return jsonify({'error': 'No assets found for SKU'}), 404

            return jsonify({
                'id': asset.id,
                'sku': asset.sku,
                'version': asset.version,
                'filename': asset.filename,
                'source': asset.source,
                'width': asset.width,
                'height': asset.height,
                'url': f'/image/{asset.id}/{Config.DEFAULT_CANONICAL_WIDTH}'
            })

        except Exception as e:
            app.logger.error(f"Error getting canonical asset for SKU {sku}: {e}")
            return jsonify({'error': 'Internal server error'}), 500
        finally:
            db.close()

    @app.route("/image/<int:asset_id>/<int:width>")
    @login_required
    def serve_dam_image(asset_id, width):
        """Serve a resized image."""
        current_user = session['username']

        try:
            db = py.utilities.models.SessionLocal()
            asset = db.query(py.utilities.models.ImageAsset).filter_by(
                id=asset_id,
                is_active=True
            ).first()

            if not asset:
                return jsonify({'error': 'Asset not found'}), 404

            if not os.path.exists(asset.path):
                return jsonify({'error': 'Image file not found'}), 404

            # Resize image on demand
            resized_image = resize_image(asset.path, width)

            if not resized_image:
                return jsonify({'error': 'Failed to resize image'}), 500

            # Return the resized image
            resized_image.seek(0)
            return send_file(resized_image, mimetype='image/jpeg')

        except Exception as e:
            app.logger.error(f"Error serving image {asset_id}: {e}")
            return jsonify({'error': 'Internal server error'}), 500
        finally:
            db.close()

    @app.route("/admin/canonical/set", methods=['POST'])
    @login_required
    def set_canonical():
        """Set the canonical image for a SKU."""
        current_user = session['username']

        # Only allow admin users
        if current_user not in ['yeti', 'yetimaster']:
            return jsonify({'error': 'Unauthorized'}), 403

        asset_id = request.form.get('asset_id', type=int)
        if not asset_id:
            return jsonify({'error': 'asset_id parameter required'}), 400

        try:
            db = py.utilities.models.SessionLocal()

            # Get the asset
            asset = db.query(py.utilities.models.ImageAsset).filter_by(id=asset_id).first()
            if not asset:
                return jsonify({'error': 'Asset not found'}), 404

            # Remove canonical flag from all assets of this SKU
            db.query(py.utilities.models.ImageAsset).filter_by(sku=asset.sku).update({'is_canonical': False})

            # Set this asset as canonical
            asset.is_canonical = True

            db.commit()

            return jsonify({'success': True, 'message': f'Set canonical image for SKU {asset.sku}'})

        except Exception as e:
            db.rollback()
            app.logger.error(f"Error setting canonical image: {e}")
            return jsonify({'error': 'Internal server error'}), 500
        finally:
            db.close()

    @app.route("/delete_image", methods=["POST"])
    @login_required
    def delete_image():
        current_user = session['username']
        if current_user != 'yetimaster':
            return jsonify({"error": "Permission denied"}), 403

        file_name = request.form.get("file_name")
        if file_name:
            try:
                original_path = os.path.join(Config.CATALOG_ORIGINAL, file_name)
                if os.path.exists(original_path):
                    os.remove(original_path)
                    app.logger.info(f"{current_user} deleted {original_path}")
                return jsonify({"message": "File deleted successfully"}), 200
            except Exception as e:
                app.logger.error(f"Error deleting file {file_name}: {e}")
                return jsonify({"error": "Error deleting file"}), 500
        else:
            return jsonify({"error": "No file name provided"}), 400