"""
Image Text Detection Service
Uses OCR to determine if an image contains significant text (catalog image) or minimal text (big picture)
"""

import cv2
import numpy as np
from PIL import Image
import logging
import os

class ImageTextDetector:
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.min_text_area_threshold = 0.05  # 5% of image should contain text for catalog
        
    def detect_text_content(self, image_path):
        """
        Detect amount of text in image using OpenCV
        Returns: dict with 'has_significant_text' (bool) and 'text_percentage' (float)
        """
        try:
            # Read image
            img = cv2.imread(image_path)
            if img is None:
                self.logger.error(f"Could not read image: {image_path}")
                return {'has_significant_text': False, 'text_percentage': 0.0, 'method': 'error'}
            
            # Get image dimensions
            height, width = img.shape[:2]
            total_pixels = height * width
            
            # Convert to grayscale
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            
            # Apply adaptive thresholding to highlight text regions
            binary = cv2.adaptiveThreshold(
                gray, 255, 
                cv2.ADAPTIVE_THRESH_GAUSSIAN_C, 
                cv2.THRESH_BINARY_INV, 
                11, 2
            )
            
            # Use morphological operations to connect text regions
            kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
            morph = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=1)
            
            # Find contours (potential text regions)
            contours, _ = cv2.findContours(morph, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            
            # Filter contours that look like text (reasonable aspect ratio and size)
            text_area = 0
            text_regions = 0
            valid_text_contours = []
            
            for contour in contours:
                x, y, w, h = cv2.boundingRect(contour)
                area = w * h
                aspect_ratio = w / h if h > 0 else 0
                
                # Text characteristics:
                # - Width should be reasonable (not too thin, not full image width)
                # - Height should be reasonable (not too short, not too tall)
                # - Aspect ratio should be text-like (horizontal orientation preferred)
                # - Size should be significant but not too large
                
                # Exclude if too large (likely texture/pattern, not text)
                if area > total_pixels * 0.15:  # Skip if larger than 15% of image
                    continue
                    
                # Exclude if too small (noise)
                if area < 100:
                    continue
                
                # Exclude if aspect ratio suggests texture rather than text
                # Text is usually 1:1 to 20:1 (width:height), but not extremely wide
                if aspect_ratio > 50 or aspect_ratio < 0.05:
                    continue
                
                # Exclude if height is too large (texture pattern)
                if h > height * 0.5:
                    continue
                
                # Exclude if width spans most of image (texture pattern)
                if w > width * 0.9:
                    continue
                
                # Check if this looks like a repeating pattern (texture)
                # by examining the solidity (filled ratio)
                contour_area = cv2.contourArea(contour)
                solidity = contour_area / area if area > 0 else 0
                
                # Text regions tend to have moderate solidity (0.3-0.8)
                # Textures often have very low (<0.1) or very high (>0.95) solidity
                if solidity < 0.15 or solidity > 0.95:
                    continue
                
                # This looks like actual text
                text_area += area
                text_regions += 1
                valid_text_contours.append((x, y, w, h))
            
            # Calculate percentage of image that contains text
            text_percentage = (text_area / total_pixels) * 100
            
            # Enhanced catalog detection:
            # Catalog images typically have:
            # - Text covering > 8% of image area (was 5%)
            # - Multiple distinct text regions (company name, phone, SKU, dimensions, etc.)
            # - At least 15 text regions (was 10)
            # OR very high text coverage (>20%)
            has_significant_text = (text_percentage > 8.0 and text_regions >= 15) or text_percentage > 20.0
            
            self.logger.info(f"Image: {image_path} | Text: {text_percentage:.2f}% | Regions: {text_regions} | Catalog: {has_significant_text}")
            
            return {
                'has_significant_text': has_significant_text,
                'text_percentage': text_percentage,
                'text_regions': text_regions,
                'method': 'opencv'
            }
            
        except Exception as e:
            self.logger.error(f"Error detecting text in {image_path}: {e}")
            return {'has_significant_text': False, 'text_percentage': 0.0, 'method': 'error'}
    
    def classify_image_type(self, image_path):
        """
        Classify image as 'catalog' or 'big_picture' based on text content
        
        Special handling for catalog sheet images (pre-made product spec sheets)
        vs product photography
        """
        result = self.detect_text_content(image_path)
        
        if result['method'] == 'error':
            # Fallback to default classification
            return 'big_picture'
        
        # Check if this is a catalog sheet based on text detection
        if result['has_significant_text']:
            return 'catalog'
        
        # If no significant text detected, check for explicit catalog indicators
        try:
            # Check for catalog indicators in filename (most reliable)
            filename_lower = os.path.basename(image_path).lower()
            has_catalog_in_name = any(word in filename_lower for word in ['catalog', 'catelog', 'spec', 'sheet'])
            
            # If filename explicitly says catalog, trust it (even without detectable text)
            # This handles pre-made catalog PDFs/images with flattened text
            if has_catalog_in_name:
                return 'catalog'
                
        except Exception as e:
            self.logger.error(f"Error checking image characteristics: {e}")
        
        # Default to big picture for everything else
        # Don't use dimension/aspect ratio heuristics as they're unreliable
        # (landscape big pictures exist, and catalog sheets vary in size)
        return 'big_picture'
    
    def batch_classify_images(self, image_paths):
        """
        Classify multiple images and return results
        Returns: dict mapping image_path -> classification details
        """
        results = {}
        for image_path in image_paths:
            text_info = self.detect_text_content(image_path)
            results[image_path] = {
                'type': 'catalog' if text_info['has_significant_text'] else 'big_picture',
                'text_percentage': text_info['text_percentage'],
                'text_regions': text_info.get('text_regions', 0),
                'confidence': self._calculate_confidence(text_info)
            }
        return results
    
    def _calculate_confidence(self, text_info):
        """
        Calculate confidence score (0-1) for the classification
        High text % = high confidence for catalog
        Low text % = high confidence for big picture
        Mid-range = lower confidence
        """
        percentage = text_info['text_percentage']
        
        if percentage > 15.0:
            return min(1.0, percentage / 25.0)  # High confidence catalog
        elif percentage < 3.0:
            return min(1.0, (3.0 - percentage) / 3.0)  # High confidence big picture
        else:
            # Mid-range: lower confidence
            return 0.5
