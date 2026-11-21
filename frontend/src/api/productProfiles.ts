import { ApiError, request, type InputRun } from './images';

export interface ProductProfile {
  asin: string;
  purpose: string[];
  style_structure: string[];
  specifications: string[];
  price: string | null;
  currency: string | null;
  package_quantity: string | null;
  unit_basis: string | null;
  price_source: 'unknown' | 'asin_current' | 'user_reference';
}

export interface ProductProfileRecord {
  project_id: string;
  run_id: string;
  version: number;
  status: 'draft' | 'confirmed';
  profile: ProductProfile;
  created_at: string;
}

const base = (run: InputRun) =>
  `/api/v1/projects/${run.project_id}/pipeline-runs/${run.id}/product-profile`;

export async function readProductProfile(run: InputRun): Promise<ProductProfileRecord | null> {
  try {
    return await request<ProductProfileRecord>(base(run));
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
}

export function generateProductProfile(
  run: InputRun,
  referencePrice?: string,
): Promise<ProductProfileRecord> {
  return request<ProductProfileRecord>(base(run), {
    method: 'POST',
    ...(referencePrice
      ? {
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ reference_price: referencePrice, currency: 'USD' }),
        }
      : {}),
  });
}

export function confirmProductProfile(
  run: InputRun,
  data: {
    expected_version: number;
    purpose: string[];
    style_structure: string[];
    specifications: string[];
    package_quantity: string;
    unit_basis: string;
    reference_price?: string;
    currency: string;
  },
): Promise<ProductProfileRecord> {
  return request<ProductProfileRecord>(base(run), {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
}
