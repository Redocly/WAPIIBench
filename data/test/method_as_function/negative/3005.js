// Test slash in path parameter (or second path parameter, respectively)
const axios = require('axios');
axios.delete(`https://petstore.swagger.io/v2/pets/4/2`);