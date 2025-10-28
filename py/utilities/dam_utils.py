"""
DAM (Digital Asset Management) utility functions
Helper functions for other modules to interact with the DAM system
"""

import py.utilities.models
from py.utilities.setting import Config


def get_asset_by_sku(sku, canonical_only=False, source_filter=None):
    """
    Get image asset(s) for a SKU
    
    Args:
        sku: The SKU to search for
        canonical_only: If True, only return canonical (default) image
        source_filter: Filter by source ('catalog', 'big_picture', 'dropbox') or list of sources
        
    Returns:
        Single asset object if canonical_only=True, list of assets otherwise
    """
    db = py.utilities.models.SessionLocal()
    try:
        query = db.query(py.utilities.models.ImageAsset).filter_by(
            sku=sku.upper().strip(),
            is_active=True
        )
        
        # Apply source filter
        if source_filter:
            if isinstance(source_filter, str):
                query = query.filter_by(source=source_filter)
            elif isinstance(source_filter, list):
                query = query.filter(py.utilities.models.ImageAsset.source.in_(source_filter))
        
        if canonical_only:
            # First try to get explicitly marked canonical
            asset = query.filter_by(is_canonical=True).first()
            # If no canonical marked, get the first version
            if not asset:
                asset = query.order_by(py.utilities.models.ImageAsset.version).first()
            return asset
        else:
            return query.order_by(py.utilities.models.ImageAsset.version).all()
    finally:
        db.close()


def get_asset_url(asset_id, width=None):
    """
    Generate URL for accessing an asset through DAM system
    
    Args:
        asset_id: The asset ID
        width: Desired width for image resizing (optional)
        
    Returns:
        URL string for accessing the asset
    """
    if width:
        return f'/image/{asset_id}/{width}'
    else:
        return f'/image/{asset_id}/{Config.DEFAULT_CANONICAL_WIDTH}'


def get_canonical_asset_url(sku, width=None, source_filter=None):
    """
    Get URL for canonical (default) image of a SKU
    
    Args:
        sku: The SKU to get image for
        width: Desired width for image resizing (optional)
        source_filter: Filter by source ('catalog', 'big_picture', 'dropbox')
        
    Returns:
        URL string for canonical image, or None if no image found
    """
    asset = get_asset_by_sku(sku, canonical_only=True, source_filter=source_filter)
    if asset:
        return get_asset_url(asset.id, width)
    return None


def search_assets_by_sku_pattern(sku_pattern, source_filter=None):
    """
    Search for assets where SKU matches a pattern
    
    Args:
        sku_pattern: Pattern to search for (case insensitive)
        source_filter: Filter by source ('catalog', 'big_picture', 'dropbox') or list of sources
        
    Returns:
        List of asset objects matching the pattern
    """
    db = py.utilities.models.SessionLocal()
    try:
        query = db.query(py.utilities.models.ImageAsset).filter(
            py.utilities.models.ImageAsset.sku.ilike(f'%{sku_pattern.upper()}%'),
            py.utilities.models.ImageAsset.is_active == True
        )
        
        # Apply source filter
        if source_filter:
            if isinstance(source_filter, str):
                query = query.filter_by(source=source_filter)
            elif isinstance(source_filter, list):
                query = query.filter(py.utilities.models.ImageAsset.source.in_(source_filter))
        
        return query.order_by(py.utilities.models.ImageAsset.sku, py.utilities.models.ImageAsset.version).all()
    finally:
        db.close()


def get_all_assets_for_skus(sku_list, source_filter=None):
    """
    Get all assets for a list of SKUs
    
    Args:
        sku_list: List of SKUs to get assets for
        source_filter: Filter by source ('catalog', 'big_picture', 'dropbox') or list of sources
        
    Returns:
        Dictionary mapping SKU -> list of assets
    """
    if not sku_list:
        return {}
        
    db = py.utilities.models.SessionLocal()
    try:
        # Convert all SKUs to uppercase for consistent lookup
        upper_skus = [sku.upper().strip() for sku in sku_list]
        
        query = db.query(py.utilities.models.ImageAsset).filter(
            py.utilities.models.ImageAsset.sku.in_(upper_skus),
            py.utilities.models.ImageAsset.is_active == True
        )
        
        # Apply source filter
        if source_filter:
            if isinstance(source_filter, str):
                query = query.filter_by(source=source_filter)
            elif isinstance(source_filter, list):
                query = query.filter(py.utilities.models.ImageAsset.source.in_(source_filter))
        
        assets = query.order_by(py.utilities.models.ImageAsset.sku, py.utilities.models.ImageAsset.version).all()
        
        # Group by SKU
        result = {}
        for asset in assets:
            if asset.sku not in result:
                result[asset.sku] = []
            result[asset.sku].append(asset)
            
        return result
    finally:
        db.close()


def get_canonical_assets_for_skus(sku_list, source_filter=None):
    """
    Get canonical (default) assets for a list of SKUs
    
    Args:
        sku_list: List of SKUs to get canonical assets for
        source_filter: Filter by source ('catalog', 'big_picture', 'dropbox') or list of sources
        
    Returns:
        Dictionary mapping SKU -> canonical asset object (or None if not found)
    """
    if not sku_list:
        return {}
        
    result = {}
    for sku in sku_list:
        result[sku.upper().strip()] = get_asset_by_sku(sku, canonical_only=True, source_filter=source_filter)
    
    return result


class DAMAssetInfo:
    """Helper class to hold asset information"""
    def __init__(self, asset):
        self.id = asset.id
        self.sku = asset.sku
        self.filename = asset.filename
        self.version = asset.version
        self.width = asset.width
        self.height = asset.height
        self.is_canonical = asset.is_canonical
        self.created_at = asset.created_at
        self.url = get_asset_url(asset.id)
        self.url_small = get_asset_url(asset.id, 400)
        self.url_medium = get_asset_url(asset.id, 800)
        self.url_large = get_asset_url(asset.id, 1200)