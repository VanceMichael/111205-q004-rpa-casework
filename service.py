def plan_vendor(row):
    tax_id = ''.join(char for char in row.get('tax_id', '') if char.isalnum()).upper()
    name = ' '.join(row.get('name', '').split())
    if not tax_id or not name:
        raise ValueError('tax id and name are required')
    return {'tax_id': tax_id, 'name': name, 'address': row.get('address', '').strip()}
