# listings/migrations/0036_add_full_category_schemas.py
from django.db import migrations, models

def populate_category_schemas(apps, schema_editor):
    Category = apps.get_model('listings', 'Category')

    # =========================================================================
    # SCHEMA MAPPING – one entry per category name (case‑insensitive match)
    # =========================================================================
    CATEGORY_SCHEMAS = {
        # 1. Art & Collectibles
        'art & collectibles': {
            'fields': [
                {'name': 'artist', 'label': 'Artist', 'type': 'text'},
                {'name': 'medium', 'label': 'Medium', 'type': 'text'},
                {'name': 'year_created', 'label': 'Year Created', 'type': 'number'},
                {'name': 'edition', 'label': 'Edition', 'type': 'text'},
                {'name': 'framed', 'label': 'Framed', 'type': 'boolean'},
            ],
            'hide_standard_fields': ['brand', 'model', 'dimensions', 'weight', 'color', 'material','condition'],
        },
        # 2. Baby & Kids Items
        'baby & kids items': {
            'fields': [
                {'name': 'age_group', 'label': 'Age Group', 'type': 'text'},
                {'name': 'material', 'label': 'Material', 'type': 'text'},
                {'name': 'safety_certified', 'label': 'Safety Certified', 'type': 'boolean'},
                {'name': 'assembly_required', 'label': 'Assembly Required', 'type': 'boolean'},
                {'name': 'batteries_included', 'label': 'Batteries Included', 'type': 'boolean'},
            ],
            'hide_standard_fields': [],  # keep brand, model, dimensions, weight, color (material already custom)
        },
        # 3. Beauty & Personal Care
        'beauty & personal care': {
            'fields': [
                {'name': 'skin_type', 'label': 'Skin Type', 'type': 'select', 'choices': ['All', 'Oily', 'Dry', 'Combination', 'Sensitive']},
                {'name': 'ingredients', 'label': 'Key Ingredients', 'type': 'textarea'},
                {'name': 'expiry_date', 'label': 'Expiry Date', 'type': 'text'},
                {'name': 'volume', 'label': 'Volume (ml)', 'type': 'number'},
                {'name': 'usage', 'label': 'How to Use', 'type': 'textarea'},
            ],
            'hide_standard_fields': [],  # keep brand, color, etc.
        },
        # 4. Boats & Marine
        'boats & marine': {
            'fields': [
                {'name': 'boat_type', 'label': 'Boat Type', 'type': 'text'},
                {'name': 'length', 'label': 'Length (ft)', 'type': 'number'},
                {'name': 'engine_type', 'label': 'Engine Type', 'type': 'text'},
                {'name': 'fuel_type', 'label': 'Fuel Type', 'type': 'select', 'choices': ['Petrol', 'Diesel', 'Electric']},
                {'name': 'year', 'label': 'Year', 'type': 'number'},
            ],
            'hide_standard_fields': ['brand', 'model', 'dimensions', 'weight', 'color', 'material'],
        },
        # 5. Cameras & Photography
        'cameras & photography': {
            'fields': [
                {'name': 'megapixels', 'label': 'Megapixels', 'type': 'number'},
                {'name': 'sensor_type', 'label': 'Sensor Type', 'type': 'text'},
                {'name': 'lens_mount', 'label': 'Lens Mount', 'type': 'text'},
                {'name': 'iso_range', 'label': 'ISO Range', 'type': 'text'},
                {'name': 'video_resolution', 'label': 'Video Resolution', 'type': 'text'},
            ],
            'hide_standard_fields': [],  # keep brand, model, etc.
        },
        # 6. Cars
        'cars': {
            'fields': [
                {'name': 'make', 'label': 'Make', 'type': 'text'},
                {'name': 'model', 'label': 'Model', 'type': 'text'},
                {'name': 'year', 'label': 'Year', 'type': 'number', 'min': 1900, 'max': 2100},
                {'name': 'mileage', 'label': 'Mileage (km)', 'type': 'number'},
                {'name': 'fuel_type', 'label': 'Fuel Type', 'type': 'select', 'choices': ['Petrol', 'Diesel', 'Electric', 'Hybrid']},
                {'name': 'transmission', 'label': 'Transmission', 'type': 'select', 'choices': ['Manual', 'Automatic']},
                {'name': 'engine_capacity', 'label': 'Engine Capacity (cc)', 'type': 'number'},
                {'name': 'color', 'label': 'Color', 'type': 'text'},
            ],
            'hide_standard_fields': ['brand', 'model', 'dimensions', 'weight', 'color', 'material'],  # we use custom make/model/color
        },
        # 7. Children's Clothing
        "children's clothing": {
            'fields': [
                {'name': 'size', 'label': 'Size', 'type': 'text'},
                {'name': 'gender', 'label': 'Gender', 'type': 'select', 'choices': ['Boy', 'Girl', 'Unisex']},
                {'name': 'age_group', 'label': 'Age Group', 'type': 'text'},
                {'name': 'fabric', 'label': 'Fabric', 'type': 'text'},
                {'name': 'season', 'label': 'Season', 'type': 'select', 'choices': ['Summer', 'Winter', 'All-weather']},
            ],
            'hide_standard_fields': ['brand', 'model', 'dimensions', 'weight', 'color', 'material'],
        },
        # 8. Commercial Properties
        'commercial properties': {
            'fields': [
                {'name': 'property_type', 'label': 'Property Type', 'type': 'select', 'choices': ['Office', 'Shop', 'Warehouse', 'Industrial']},
                {'name': 'area_sqft', 'label': 'Area (sq ft)', 'type': 'number'},
                {'name': 'parking', 'label': 'Parking Available', 'type': 'boolean'},
                {'name': 'lease_term', 'label': 'Lease Term', 'type': 'text'},
                {'name': 'zoning', 'label': 'Zoning', 'type': 'text'},
            ],
            'hide_standard_fields': ['brand', 'model', 'dimensions', 'weight', 'color', 'material'],
        },
        # 9. Computers & Laptops
        'computers & laptops': {
            'fields': [
                {'name': 'processor', 'label': 'Processor', 'type': 'text'},
                {'name': 'ram', 'label': 'RAM (GB)', 'type': 'number'},
                {'name': 'storage', 'label': 'Storage (GB/Type)', 'type': 'text'},
                {'name': 'graphics', 'label': 'Graphics Card', 'type': 'text'},
                {'name': 'screen_size', 'label': 'Screen Size', 'type': 'text'},
                {'name': 'operating_system', 'label': 'OS', 'type': 'text'},
            ],
            'hide_standard_fields': [],
        },
        # 10. Construction
        'construction': {
            'fields': [
                {'name': 'tool_type', 'label': 'Tool Type', 'type': 'text'},
                {'name': 'power_source', 'label': 'Power Source', 'type': 'select', 'choices': ['Manual', 'Electric', 'Battery', 'Petrol']},
                {'name': 'voltage', 'label': 'Voltage', 'type': 'text'},
                {'name': 'warranty', 'label': 'Warranty', 'type': 'text'},
                {'name': 'certifications', 'label': 'Certifications', 'type': 'textarea'},
            ],
            'hide_standard_fields': [],
        },
        # 11. Crops & Seeds
        'crops & seeds': {
            'fields': [
                {'name': 'crop_type', 'label': 'Crop Type', 'type': 'text'},
                {'name': 'variety', 'label': 'Variety', 'type': 'text'},
                {'name': 'planting_season', 'label': 'Planting Season', 'type': 'text'},
                {'name': 'germination_rate', 'label': 'Germination Rate (%)', 'type': 'number'},
                {'name': 'organic', 'label': 'Organic', 'type': 'boolean'},
            ],
            'hide_standard_fields': ['brand', 'model', 'dimensions', 'weight', 'color', 'material'],
        },
        # 12. Education & Classes
        'education & classes': {
            'fields': [
                {'name': 'course_name', 'label': 'Course Name', 'type': 'text'},
                {'name': 'instructor', 'label': 'Instructor', 'type': 'text'},
                {'name': 'duration', 'label': 'Duration', 'type': 'text'},
                {'name': 'mode', 'label': 'Mode', 'type': 'select', 'choices': ['Online', 'In-person', 'Hybrid']},
                {'name': 'certificate', 'label': 'Certificate Offered', 'type': 'boolean'},
            ],
            'hide_standard_fields': ['brand', 'model', 'dimensions', 'weight', 'color', 'material'],
        },
        # 13. Farming Equipment
        'farming equipment': {
            'fields': [
                {'name': 'equipment_type', 'label': 'Equipment Type', 'type': 'text'},
                {'name': 'power_source', 'label': 'Power Source', 'type': 'select', 'choices': ['Manual', 'Diesel', 'Electric']},
                {'name': 'horsepower', 'label': 'Horsepower', 'type': 'number'},
                {'name': 'width', 'label': 'Working Width', 'type': 'text'},
                {'name': 'year', 'label': 'Year', 'type': 'number'},
            ],
            'hide_standard_fields': [],
        },
        # 14. Fishing Equipment
        'fishing equipment': {
            'fields': [
                {'name': 'fishing_type', 'label': 'Fishing Type', 'type': 'select', 'choices': ['Freshwater', 'Saltwater', 'Fly', 'Ice']},
                {'name': 'rod_length', 'label': 'Rod Length', 'type': 'text'},
                {'name': 'reel_type', 'label': 'Reel Type', 'type': 'text'},
                {'name': 'line_test', 'label': 'Line Test (lb)', 'type': 'number'},
                {'name': 'material', 'label': 'Material', 'type': 'text'},
            ],
            'hide_standard_fields': [],
        },
        # 15. Food & Beverages
        'food & beverages': {
            'fields': [
                {'name': 'food_type', 'label': 'Food Type', 'type': 'text'},
                {'name': 'pack_size', 'label': 'Pack Size', 'type': 'text'},
                {'name': 'ingredients', 'label': 'Ingredients', 'type': 'textarea'},
                {'name': 'expiry_date', 'label': 'Expiry Date', 'type': 'text'},
                {'name': 'storage', 'label': 'Storage Instructions', 'type': 'textarea'},
            ],
            'hide_standard_fields': ['brand', 'model', 'dimensions', 'weight', 'color', 'material'],
        },
        # 16. Furniture
        'furniture': {
            'fields': [
                {'name': 'room', 'label': 'Room', 'type': 'select', 'choices': ['Living Room', 'Bedroom', 'Kitchen', 'Dining', 'Office']},
                {'name': 'material', 'label': 'Material', 'type': 'text'},
                {'name': 'assembly_required', 'label': 'Assembly Required', 'type': 'boolean'},
                {'name': 'style', 'label': 'Style', 'type': 'text'},
                {'name': 'finish', 'label': 'Finish', 'type': 'text'},
            ],
            'hide_standard_fields': [],
        },
        # 17. Health & Wellness
        'health & wellness': {
            'fields': [
                {'name': 'product_type', 'label': 'Product Type', 'type': 'text'},
                {'name': 'ingredients', 'label': 'Ingredients', 'type': 'textarea'},
                {'name': 'expiry_date', 'label': 'Expiry Date', 'type': 'text'},
                {'name': 'dosage', 'label': 'Dosage', 'type': 'text'},
                {'name': 'side_effects', 'label': 'Side Effects', 'type': 'textarea'},
            ],
            'hide_standard_fields': [],
        },
        # 18. Home Appliances
        'home appliances': {
            'fields': [
                {'name': 'appliance_type', 'label': 'Appliance Type', 'type': 'text'},
                {'name': 'power_rating', 'label': 'Power Rating (W)', 'type': 'number'},
                {'name': 'energy_efficiency', 'label': 'Energy Efficiency', 'type': 'text'},
                {'name': 'capacity', 'label': 'Capacity', 'type': 'text'},
                {'name': 'installation_required', 'label': 'Installation Required', 'type': 'boolean'},
            ],
            'hide_standard_fields': [],
        },
        # 19. Home Decor
        'home decor': {
            'fields': [
                {'name': 'decor_type', 'label': 'Decor Type', 'type': 'text'},
                {'name': 'material', 'label': 'Material', 'type': 'text'},
                {'name': 'style', 'label': 'Style', 'type': 'text'},
                {'name': 'color', 'label': 'Color', 'type': 'text'},
                {'name': 'theme', 'label': 'Theme', 'type': 'text'},
            ],
            'hide_standard_fields': [],
        },
        # 20. Houses for Rent
        'houses for rent': {
            'fields': [
                {'name': 'property_type', 'label': 'Property Type', 'type': 'select', 'choices': ['House', 'Apartment', 'Bedsitter']},
                {'name': 'bedrooms', 'label': 'Bedrooms', 'type': 'number'},
                {'name': 'bathrooms', 'label': 'Bathrooms', 'type': 'number'},
                {'name': 'furnished', 'label': 'Furnished', 'type': 'boolean'},
                {'name': 'parking', 'label': 'Parking', 'type': 'boolean'},
                {'name': 'rent_per_month', 'label': 'Rent per Month', 'type': 'number'},
            ],
            'hide_standard_fields': ['brand', 'model', 'dimensions', 'weight', 'color', 'material'],
        },
        # 21. Houses for Sale
        'houses for sale': {
            'fields': [
                {'name': 'property_type', 'label': 'Property Type', 'type': 'select', 'choices': ['House', 'Apartment', 'Villa']},
                {'name': 'bedrooms', 'label': 'Bedrooms', 'type': 'number'},
                {'name': 'bathrooms', 'label': 'Bathrooms', 'type': 'number'},
                {'name': 'land_size', 'label': 'Land Size', 'type': 'text'},
                {'name': 'furnished', 'label': 'Furnished', 'type': 'boolean'},
                {'name': 'parking', 'label': 'Parking', 'type': 'boolean'},
            ],
            'hide_standard_fields': ['brand', 'model', 'dimensions', 'weight', 'color', 'material'],
        },
        # 22. Jewelry & Watches
        'jewelry & watches': {
            'fields': [
                {'name': 'metal_type', 'label': 'Metal Type', 'type': 'text'},
                {'name': 'stone_type', 'label': 'Stone Type', 'type': 'text'},
                {'name': 'stone_carat', 'label': 'Stone Carat', 'type': 'number'},
                {'name': 'length', 'label': 'Length (cm)', 'type': 'text'},
                {'name': 'clasp_type', 'label': 'Clasp Type', 'type': 'text'},
            ],
            'hide_standard_fields': ['brand', 'model', 'dimensions', 'weight', 'color', 'material'],
        },
        # 23. Job Offers
        'job offers': {
            'fields': [
                {'name': 'job_type', 'label': 'Job Type', 'type': 'select', 'choices': ['Full-time', 'Part-time', 'Contract', 'Temporary']},
                {'name': 'experience', 'label': 'Experience Required', 'type': 'text'},
                {'name': 'education', 'label': 'Education Level', 'type': 'text'},
                {'name': 'salary_range', 'label': 'Salary Range', 'type': 'text'},
                {'name': 'benefits', 'label': 'Benefits', 'type': 'textarea'},
            ],
            'hide_standard_fields': ['brand', 'model', 'dimensions', 'weight', 'color', 'material'],
        },
        # 24. Kitchenware
        'kitchenware': {
            'fields': [
                {'name': 'material', 'label': 'Material', 'type': 'text'},
                {'name': 'non_stick', 'label': 'Non‑stick', 'type': 'boolean'},
                {'name': 'dishwasher_safe', 'label': 'Dishwasher Safe', 'type': 'boolean'},
                {'name': 'microwave_safe', 'label': 'Microwave Safe', 'type': 'boolean'},
                {'name': 'set_size', 'label': 'Set Size', 'type': 'number'},
            ],
            'hide_standard_fields': [],
        },
        # 25. Land & Plots
        'land & plots': {
            'fields': [
                {'name': 'land_type', 'label': 'Land Type', 'type': 'select', 'choices': ['Residential', 'Commercial', 'Agricultural']},
                {'name': 'area_sqft', 'label': 'Area (sq ft)', 'type': 'number'},
                {'name': 'survey_number', 'label': 'Survey Number', 'type': 'text'},
                {'name': 'title_deed', 'label': 'Title Deed Available', 'type': 'boolean'},
                {'name': 'utilities', 'label': 'Utilities Available', 'type': 'text'},
            ],
            'hide_standard_fields': ['brand', 'model', 'dimensions', 'weight', 'color', 'material'],
        },
        # 26. Livestock & Poultry
        'livestock & poultry': {
            'fields': [
                {'name': 'animal_type', 'label': 'Animal Type', 'type': 'text'},
                {'name': 'breed', 'label': 'Breed', 'type': 'text'},
                {'name': 'age', 'label': 'Age (months)', 'type': 'number'},
                {'name': 'weight', 'label': 'Weight (kg)', 'type': 'number'},
                {'name': 'health_certified', 'label': 'Health Certified', 'type': 'boolean'},
            ],
            'hide_standard_fields': ['brand', 'model', 'dimensions', 'weight', 'color', 'material'],
        },
        # 27. Men's Clothing
        "men's clothing": {
            'fields': [
                {'name': 'size', 'label': 'Size', 'type': 'text'},
                {'name': 'fit', 'label': 'Fit', 'type': 'select', 'choices': ['Slim', 'Regular', 'Loose']},
                {'name': 'fabric', 'label': 'Fabric', 'type': 'text'},
                {'name': 'occasion', 'label': 'Occasion', 'type': 'text'},
                {'name': 'season', 'label': 'Season', 'type': 'select', 'choices': ['Summer', 'Winter', 'All-weather']},
            ],
            'hide_standard_fields': ['brand', 'model', 'dimensions', 'weight', 'color', 'material'],
        },
        # 28. Mobile Phones & Tablets
        'mobile phones & tablets': {
            'fields': [
                {'name': 'processor', 'label': 'Processor', 'type': 'text'},
                {'name': 'ram', 'label': 'RAM (GB)', 'type': 'number'},
                {'name': 'storage', 'label': 'Storage (GB)', 'type': 'number'},
                {'name': 'screen_size', 'label': 'Screen Size (inches)', 'type': 'text'},
                {'name': 'battery_capacity', 'label': 'Battery Capacity (mAh)', 'type': 'number'},
                {'name': 'network', 'label': 'Network (4G/5G)', 'type': 'text'},
            ],
            'hide_standard_fields': [],
        },
        # 29. Motorcycles & Scooters
        'motorcycles & scooters': {
            'fields': [
                {'name': 'make', 'label': 'Make', 'type': 'text'},
                {'name': 'model', 'label': 'Model', 'type': 'text'},
                {'name': 'year', 'label': 'Year', 'type': 'number'},
                {'name': 'engine_cc', 'label': 'Engine (cc)', 'type': 'number'},
                {'name': 'mileage', 'label': 'Mileage (km)', 'type': 'number'},
                {'name': 'color', 'label': 'Color', 'type': 'text'},
            ],
            'hide_standard_fields': ['brand', 'model', 'dimensions', 'weight', 'color', 'material'],
        },
        # 30. Musical Instruments
        'musical instruments': {
            'fields': [
                {'name': 'instrument_type', 'label': 'Instrument Type', 'type': 'text'},
                {'name': 'brand', 'label': 'Brand', 'type': 'text'},
                {'name': 'model', 'label': 'Model', 'type': 'text'},
                {'name': 'material', 'label': 'Material', 'type': 'text'},
                {'name': 'includes_case', 'label': 'Includes Case', 'type': 'boolean'},
            ],
            'hide_standard_fields': [],
        },
        # 31. Services
        'services': {
            'fields': [
                {'name': 'service_type', 'label': 'Service Type', 'type': 'text'},
                {'name': 'duration', 'label': 'Duration', 'type': 'text'},
                {'name': 'location', 'label': 'Service Location', 'type': 'text'},
                {'name': 'certifications', 'label': 'Certifications', 'type': 'textarea'},
            ],
            'hide_standard_fields': ['brand', 'model', 'dimensions', 'weight', 'color', 'material'],
        },
        # 32. Sports Equipment
        'sports equipment': {
            'fields': [
                {'name': 'sport_type', 'label': 'Sport Type', 'type': 'text'},
                {'name': 'size', 'label': 'Size', 'type': 'text'},
                {'name': 'material', 'label': 'Material', 'type': 'text'},
                {'name': 'weight', 'label': 'Weight (kg)', 'type': 'number'},
                {'name': 'skill_level', 'label': 'Skill Level', 'type': 'select', 'choices': ['Beginner', 'Intermediate', 'Professional']},
            ],
            'hide_standard_fields': [],
        },
        # 33. TV, Audio & Video
        'tv, audio & video': {
            'fields': [
                {'name': 'device_type', 'label': 'Device Type', 'type': 'select', 'choices': ['TV', 'Speaker', 'Home Theater', 'Streaming Device']},
                {'name': 'screen_size', 'label': 'Screen Size (inches)', 'type': 'number'},
                {'name': 'resolution', 'label': 'Resolution', 'type': 'text'},
                {'name': 'connectivity', 'label': 'Connectivity', 'type': 'text'},
                {'name': 'power_output', 'label': 'Power Output (W)', 'type': 'number'},
            ],
            'hide_standard_fields': [],
        },
        # 34. Vehicle Parts & Accessories
        'vehicle parts & accessories': {
            'fields': [
                {'name': 'part_type', 'label': 'Part Type', 'type': 'text'},
                {'name': 'compatible_make', 'label': 'Compatible Make', 'type': 'text'},
                {'name': 'compatible_model', 'label': 'Compatible Model', 'type': 'text'},
                {'name': 'year_range', 'label': 'Year Range', 'type': 'text'},
                {'name': 'oem_number', 'label': 'OEM Number', 'type': 'text'},
            ],
            'hide_standard_fields': [],
        },
        # 35. Women's Clothing
        "women's clothing": {
            'fields': [
                {'name': 'size', 'label': 'Size', 'type': 'text'},
                {'name': 'fit', 'label': 'Fit', 'type': 'select', 'choices': ['Slim', 'Regular', 'Loose']},
                {'name': 'fabric', 'label': 'Fabric', 'type': 'text'},
                {'name': 'occasion', 'label': 'Occasion', 'type': 'text'},
                {'name': 'season', 'label': 'Season', 'type': 'select', 'choices': ['Summer', 'Winter', 'All-weather']},
            ],
            'hide_standard_fields': ['brand', 'model', 'dimensions', 'weight', 'color', 'material'],
        },
    }

    # =========================================================================
    # APPLY SCHEMAS TO EXISTING CATEGORIES
    # =========================================================================
    updated_count = 0
    for cat in Category.objects.all():
        key = cat.name.strip().lower()
        schema = CATEGORY_SCHEMAS.get(key)

        # If exact match fails, try a partial match (e.g., "Art & Collectibles" contains "art")
        if not schema:
            for pattern, s in CATEGORY_SCHEMAS.items():
                if pattern in key or key in pattern:
                    schema = s
                    break

        if not schema:
            # Fallback: no dynamic fields, keep all standard fields visible
            schema = {'fields': [], 'hide_standard_fields': []}
            print(f"Warning: No specific schema for '{cat.name}'. Using empty fallback.")

        # Only set if fields_schema is empty (or you can force update by removing this condition)
        if not cat.fields_schema:
            cat.fields_schema = schema
            cat.save(update_fields=['fields_schema'])
            updated_count += 1
        else:
            # If schema exists but hide_standard_fields is missing, add it
            if 'hide_standard_fields' not in cat.fields_schema:
                cat.fields_schema['hide_standard_fields'] = schema.get('hide_standard_fields', [])
                cat.save(update_fields=['fields_schema'])
                updated_count += 1

    print(f"Updated {updated_count} categories with schemas.")


class Migration(migrations.Migration):
    dependencies = [
        ('listings', '0035_category_schema_group'),  # ← REPLACE WITH THE CORRECT PREVIOUS MIGRATION NAME
    ]

    operations = [
        migrations.RunPython(populate_category_schemas, reverse_code=migrations.RunPython.noop),
    ]