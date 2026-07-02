// Test nonexistent server
const axios = require('axios');
axios.get('https://petstore.swagger.io/v1/pets/42');