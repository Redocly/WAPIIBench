// Test get request with string interpolation in path
const axios = require('axios');
const id = 42
axios.get(`https://petstore.swagger.io/v2/pets/${id}`);