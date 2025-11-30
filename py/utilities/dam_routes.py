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
    def serve_dam_image(asset_id, width):
        """Serve a resized image."""
        # Allow access for internal user or TK user
        current_user = session.get('username')
        tk_user = session.get('tk_user', False)
        if not current_user and not tk_user:
            return jsonify({'error': 'Unauthorized'}), 403

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

            # If width is 0, serve original image
            if width == 0:
                # Determine MIME type based on file extension
                import mimetypes
                mime_type, _ = mimetypes.guess_type(asset.path)
                if not mime_type:
                    mime_type = 'application/octet-stream'
                
                return send_file(asset.path, mimetype=mime_type, as_attachment=False)
            
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
        if not file_name:
            return jsonify({"error": "No file name provided"}), 400

        try:
            db = py.utilities.models.SessionLocal()
            
            # Find the asset by filename
            asset = db.query(py.utilities.models.ImageAsset).filter_by(
                filename=file_name,
                is_active=True
            ).first()
            
            if not asset:
                return jsonify({"error": "Asset not found in DAM system"}), 404
            
            # Soft delete: mark as inactive and set deleted_at timestamp
            from datetime import datetime
            asset.is_active = False
            asset.deleted_at = datetime.utcnow()
            
            db.commit()
            app.logger.info(f"{current_user} soft-deleted asset {asset.id} ({file_name})")
            
            return jsonify({"message": "Image deleted successfully"}), 200
            
        except Exception as e:
            db.rollback()
            app.logger.error(f"Error deleting image {file_name}: {e}")
            return jsonify({"error": "Error deleting image"}), 500
        finally:
            db.close()