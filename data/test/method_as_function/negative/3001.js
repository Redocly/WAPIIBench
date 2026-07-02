// Test request body for request that must not have one
const axios = require('axios');
const id = 42
axios.delete(`https://petstore.swagger.io/v2/pets/${id}`, {
    name: 'name',
    tag: 'tag'
});