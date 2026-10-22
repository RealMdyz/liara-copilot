// Minimal "database" layer. A real app would connect using DATABASE_URL;
// here we only require it to be configured, to reproduce a classic
// "missing environment variable -> HTTP 500" production incident.
const PRODUCTS = [
  { id: 1, name: 'Phone X', price: 24_900_000 },
  { id: 2, name: 'Laptop Pro', price: 61_500_000 },
  { id: 3, name: 'Phone Mini', price: 12_300_000 },
  { id: 4, name: 'Headphones', price: 3_400_000 },
];

function getDb() {
  if (!process.env.DATABASE_URL) {
    throw new Error('DATABASE_URL is not set. Cannot connect to database');
  }
  return { products: () => PRODUCTS };
}

module.exports = { getDb };
