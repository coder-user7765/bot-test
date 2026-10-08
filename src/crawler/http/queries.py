"""GraphQL documents.

The public Ouedkniss pages are a JavaScript shell: the HTML contains no listings. The site's
own front-end reads its data from https://api.ouedkniss.com/graphql, using the documents below
(trimmed copies of the ones shipped in the site's public JS bundles). Only publicly visible
fields are requested: no phone numbers, e-mail addresses, usernames, or messenger links.
"""

MENU_QUERY = """
query listingMenu($menuFilter: MenuFilterInput) {
  listingMenu: menuFetch(menuFilter: $menuFilter) {
    id
    name
    target {
      ... on Category { id name slug }
      ... on TargetLink { link }
    }
    rank
  }
}
"""

CATEGORY_CHILDREN_QUERY = """
query CategoryChildren($q: String, $filter: SearchFilterInput) {
  search(q: $q, filter: $filter) {
    active { category { id name slug children { id name slug } } count }
  }
}
"""

SEARCH_QUERY = """
query SearchAnnouncementsQuery($q: String, $filter: SearchFilterInput) {
  search(q: $q, filter: $filter) {
    announcements {
      data {
        id
        title
        slug
        createdAt
        refreshedAt
        description
        status
        isFromStore
        hasDelivery
        deliveryType
        price
        pricePreview
        priceUnit
        priceType
        oldPrice
        exchangeType
        category { id slug }
        cities { id name region { id name slug } }
        store { id name slug isOfficial isVerified }
        defaultMedia(size: MEDIUM) { mediaUrl mimeType }
        medias(size: SMALL) { mediaUrl mimeType }
        smallDescription { specification { codename } valueText }
      }
      paginatorInfo { currentPage lastPage hasMorePages total }
    }
  }
}
"""

DETAIL_QUERY = """
query AnnouncementGet($id: ID!) {
  announcement: announcementDetails(id: $id) {
    id
    reference
    title
    slug
    description
    createdAt
    refreshedAt
    price
    pricePreview
    oldPrice
    priceType
    exchangeType
    priceUnit
    hasDelivery
    deliveryType
    hasPhone
    hasEmail
    quantity
    status
    street_name
    category {
      id slug name deliveryType
      parentTree { id name slug }
    }
    defaultMedia(size: ORIGINAL) { mediaUrl mimeType }
    medias(size: LARGE) { mediaUrl mimeType }
    categories { id name slug parentId }
    specs {
      specification { label codename type }
      value
      valueText
    }
    user { id displayName }
    isFromStore
    store {
      id name slug description url followerCount announcementsCount status
      locations { location { address region { slug name } } }
      categories { name slug }
    }
    cities { id name region { id name slug } }
    variants {
      id
      specifications { specification { codename label } valueText value }
      price oldPrice pricePreview quantity
    }
  }
}
"""
